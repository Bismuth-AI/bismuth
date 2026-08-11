"""The ``_folder.md`` beside every folder: a one-line note on what it holds."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bismuth.domain.errors import CharterError
from bismuth.domain.maintenance import is_axis_label

#: Filename of a folder's note; sorts to the top of a listing.
CHARTER_FILENAME = "_folder.md"
CHARTER_SCHEMA_VERSION = 5
MAX_PURPOSE_CHARS = 220

_GENERATED_BODY_NOTICE = "<!-- generated from frontmatter -->"


def routing_purpose(value: str, *, fallback: str) -> str:
    """Return a bounded, one-line routing sign without trusting model obedience.

    A folder purpose is a stable boundary contract, not an inventory paragraph.  Prompts
    ask for a short sign, but provider output is untrusted: rejecting an overlong sign
    caused schema-repair calls and could make an already-filed document look failed.
    Existing callers therefore keep a known sign, while a newly-created boundary falls
    back to its already validated class name.  The fallback is capped only as a final
    filesystem-safe guard; model prose is never blindly truncated into a broken sentence.
    """
    normalised = " ".join(value.split()).strip()
    if normalised and len(normalised) <= MAX_PURPOSE_CHARS:
        return normalised
    safe = " ".join(fallback.split()).strip()
    return safe[:MAX_PURPOSE_CHARS] or "?"


class Charter(BaseModel):
    """The note for a single folder."""

    model_config = ConfigDict(frozen=True)

    path: PurePosixPath = Field(description="Vault-relative folder path. Empty path is the root.")
    title: str
    purpose: str = Field(description="One line: what this folder holds. The routing hint.")
    holds: tuple[str, ...] = Field(
        default=(),
        description="A few concrete examples of what belongs here, to steer future filing.",
    )
    answers: tuple[str, ...] = Field(
        default=(),
        description="Questions whose answers live in this subtree. Read by an agent deciding to descend.",
    )
    managed: bool = Field(
        default=True,
        description="False for a note a human wrote. Bismuth reads those but never rewrites them.",
    )
    split_basis: str = Field(
        default="",
        description=(
            "The distinction this folder was divided along, in its own words. Empty when "
            "it has never been divided. Read back when asking whether the division still "
            "holds -- without it the only question available is 'how would you divide "
            "this', which has an answer every time and so never settles."
        ),
    )
    split_question: str = Field(
        default="",
        description=(
            "The question every child folder answers. Kept separately from the short "
            "axis label so human prose can never become machine classification state."
        ),
    )
    split_at_documents: int = Field(
        default=0,
        ge=0,
        description=(
            "How many documents were under here, at any depth, when it was divided. The "
            "division is reconsidered once that number has doubled: a judgement made from "
            "thirty is not worth revisiting at thirty-one. Counted through the subtree, not "
            "directly: dividing empties the folder into its children, so a direct count "
            "would drop to zero on the way out and the division would never be looked at "
            "again however much grew beneath it."
        ),
    )
    boundary_review_required: bool = Field(
        default=False,
        description="True when an older boundary must pass the current complete-review contract.",
    )
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("split_basis")
    @classmethod
    def _axis_is_a_label(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("split_basis must be a single-line axis label")
        value = " ".join(value.split()).strip()
        if value and not is_axis_label(value):
            raise ValueError("split_basis must be a short axis label, not an explanation")
        return value

    @property
    def divided(self) -> bool:
        return bool(self.split_basis)

    def due_for_review(self, documents_now: int) -> bool:
        """Whether the evidence has doubled since this folder was divided.

        ``documents_now`` is the count through the subtree, matching what was recorded.

        This rations the one question that may move a document that is already filed.
        Growing a new class out of the loose pile is not rationed at all: it was, on a
        power-of-two schedule, and a thirty-document archive got four questions -- all
        of them early, all correctly declined, and none after the sixteenth document.

        Scheduling, not judgement: asking late costs a late fix, never a wrong tree
        (SPEC.md 6.1). The ratio is to the folder's own history, so nothing here is
        tuned to a corpus.
        """
        if self.divided and self.boundary_review_required:
            return True
        if not self.divided or self.split_at_documents <= 0:
            return False
        return documents_now >= self.split_at_documents * 2

    def to_markdown(self) -> str:
        meta: dict[str, Any] = {
            "bismuth_charter": CHARTER_SCHEMA_VERSION,
            "managed": self.managed,
            "updated_at": self.updated_at.isoformat(),
            "title": self.title,
            "purpose": self.purpose,
        }
        if self.divided:
            meta["split_basis"] = self.split_basis
            meta["split_question"] = self.split_question
            meta["split_at_documents"] = self.split_at_documents
            if self.boundary_review_required:
                meta["boundary_review_required"] = True
        front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
        return f"---\n{front}---\n\n{self._render_body()}\n"

    def _render_body(self) -> str:
        lines: list[str] = [_GENERATED_BODY_NOTICE, "", f"# {self.title}", "", self.purpose, ""]
        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def from_markdown(cls, text: str, *, path: PurePosixPath) -> Charter:
        """Parse a note from disk. Only the frontmatter is read.

        Raises:
            CharterError: if the frontmatter is missing or unusable.
        """
        meta = _split_frontmatter(text)
        if meta is None:
            raise CharterError(f"{path or '<root>'}/{CHARTER_FILENAME}: no YAML frontmatter")

        version = meta.get("bismuth_charter")
        if version is None:
            raise CharterError(f"{path or '<root>'}/{CHARTER_FILENAME}: not a Bismuth folder note")
        if not isinstance(version, int) or version > CHARTER_SCHEMA_VERSION:
            raise CharterError(
                f"{path or '<root>'}/{CHARTER_FILENAME}: schema version {version!r} is newer "
                f"than this Bismuth understands. Upgrade Bismuth."
            )

        # Older schemas did not guarantee that changing an axis also replaced the old
        # child tree. Preserve the evidence, but force it through the complete review
        # contract before treating the boundary as settled.
        legacy_basis = str(meta.get("split_basis") or "")

        try:
            return cls(
                path=path,
                title=str(meta.get("title") or (path.name if path.name else "Vault root")),
                purpose=str(meta.get("purpose") or ""),
                holds=tuple(str(x) for x in meta.get("holds") or ()),
                answers=tuple(str(x) for x in meta.get("answers") or ()),
                managed=bool(meta.get("managed", True)),
                split_basis=legacy_basis,
                split_question=str(meta.get("split_question") or ""),
                split_at_documents=int(meta.get("split_at_documents") or 0),
                boundary_review_required=bool(
                    meta.get("boundary_review_required", False)
                    or (version < CHARTER_SCHEMA_VERSION and legacy_basis)
                ),
                updated_at=_parse_datetime(meta.get("updated_at")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CharterError(f"{path or '<root>'}/{CHARTER_FILENAME}: malformed ({exc})") from exc


def _split_frontmatter(text: str) -> dict[str, Any] | None:
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].rstrip() in ("---", "..."))
    except StopIteration:
        return None
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)
