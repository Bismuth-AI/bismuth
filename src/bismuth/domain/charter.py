"""The ``_folder.md`` beside every folder: a one-line note on what it holds."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bismuth.domain.errors import CharterError
from bismuth.domain.maintenance import is_axis_label, normalise_label

#: Filename of a folder's note; sorts to the top of a listing.
CHARTER_FILENAME = "_folder.md"
CHARTER_SCHEMA_VERSION = 6
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


def boundary_purpose(axis: str, class_name: str) -> str:
    """The last-resort child sign, derived from machine-owned boundary state alone.

    Used when no usable sign was proposed.  It says nothing the folder name does not
    already say -- the axis is identical across every sibling, so ``axis: name`` cannot
    rule anything out.  That is acceptable as a fallback and was not acceptable as the
    only mechanism: a 300-document run wrote it everywhere and review then answered
    "these signs do not help a reader rule alternatives out" in 25 of 26 packets,
    correctly.  Prefer :func:`routing_sign`.
    """
    clean_axis = " ".join(axis.split()).strip()
    clean_name = " ".join(class_name.split()).strip()
    if clean_axis and clean_name:
        return f"{clean_axis}: {clean_name}"
    return clean_name or clean_axis or "?"


#: Request-local handles the model is shown. They mean nothing outside that one request,
#: and they have been observed inside model-written notes. Not anchored with ``\b``: in a
#: language that does not space its particles, ``D0001과`` has no word boundary after the
#: digits, and that is exactly the shape the leak took.
_REQUEST_HANDLE = re.compile(r"(?<![A-Za-z0-9])[DFG]\d{3,4}(?!\d)")


def routing_sign(proposed: str, *, axis: str, class_name: str) -> str:
    """A child's sign: what belongs here that does not belong behind a sibling.

    [SPEC.md 3.6] makes the note the only thing a reader has for narrowing candidates,
    so it has to be able to exclude.  ADR-0014 replaced model prose with a derived
    string because the model had written request-local handles, exclusions and its own
    decision process into a public file; that removed the leak and the information with
    it.

    So the prose comes back, but never as something that can fail an ingest or outlive
    its meaning: anything carrying a request-local handle, spanning lines, or running
    past the sign budget falls back to the derived form.  A fallback sign is worse; a
    wrong one is a lie on disk, and a failed one loses a document that is already safe.
    """
    if sign_refusal(proposed, class_name=class_name) is None:
        return " ".join(proposed.split()).strip()
    return boundary_purpose(axis, class_name)


def sign_refusal(proposed: str, *, class_name: str) -> str | None:
    """Why this sign cannot go on disk, or ``None`` when it can.

    Split out of ``routing_sign`` so the caller can say what happened. The fallback is a
    folder note that repeats its own name in other words, and ``boundary_purpose`` says
    plainly that such a note rules nothing out -- so a run where it appears often has a
    real defect. Measured on 300 documents: eight of twenty-seven divisions wrote the
    fallback, and nothing recorded which of these four conditions had rejected the sign.
    """
    normalised = " ".join(proposed.split()).strip()
    if not normalised:
        return "no sign was proposed"
    if len(normalised) > MAX_PURPOSE_CHARS:
        return f"sign is {len(normalised)} characters, past the {MAX_PURPOSE_CHARS} budget"
    if _REQUEST_HANDLE.search(normalised):
        return "sign carries a request-local document handle"
    # A sign that only repeats the folder name excludes nothing, which is the whole
    # job. Observed live: a model asked for name then sign returned "지침" for both.
    if normalise_label(normalised) == normalise_label(class_name):
        return "sign is the folder name again"
    return None


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
    last_review_at_documents: int = Field(
        default=0,
        ge=0,
        description=(
            "Subtree size at the last completed review attempt, including a rejected "
            "repair. Prevents the same failed redesign from running on every arrival."
        ),
    )
    repair_pending: bool = Field(
        default=False,
        description=(
            "The current boundary failed review but no validated replacement was safe "
            "to apply. Filing may continue while repair waits for materially new evidence."
        ),
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
        if not self.divided:
            return False
        if self.boundary_review_required and self.last_review_at_documents <= 0:
            return True
        baseline = max(self.split_at_documents, self.last_review_at_documents)
        if baseline <= 0:
            return False
        return documents_now >= baseline * 2

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
            if self.last_review_at_documents:
                meta["last_review_at_documents"] = self.last_review_at_documents
            if self.repair_pending:
                meta["repair_pending"] = True
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
                last_review_at_documents=int(meta.get("last_review_at_documents") or 0),
                repair_pending=bool(meta.get("repair_pending", False)),
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
