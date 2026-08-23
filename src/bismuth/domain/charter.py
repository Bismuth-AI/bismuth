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
CHARTER_SCHEMA_VERSION = 8
MAX_PURPOSE_CHARS = 220

_GENERATED_BODY_NOTICE = "<!-- generated from frontmatter -->"


def routing_purpose(value: str, *, fallback: str) -> str:
    """Return a bounded one-line purpose, falling back to a validated name."""
    normalised = " ".join(value.split()).strip()
    if normalised and len(normalised) <= MAX_PURPOSE_CHARS:
        return normalised
    safe = " ".join(fallback.split()).strip()
    return safe[:MAX_PURPOSE_CHARS] or "?"


def boundary_purpose(axis: str, class_name: str) -> str:
    """Build a fallback purpose from validated boundary state."""
    clean_axis = " ".join(axis.split()).strip()
    clean_name = " ".join(class_name.split()).strip()
    if clean_axis and clean_name:
        return f"{clean_axis}: {clean_name}"
    return clean_name or clean_axis or "?"


#: Request-local handles must not be persisted in folder notes.
_REQUEST_HANDLE = re.compile(r"(?<![A-Za-z0-9])[DFG]\d{3,4}(?!\d)")


def routing_sign(proposed: str, *, axis: str, class_name: str) -> str:
    """Return a safe routing sign or a deterministic fallback."""
    if sign_refusal(proposed, class_name=class_name) is None:
        return " ".join(proposed.split()).strip()
    return boundary_purpose(axis, class_name)


def sign_refusal(proposed: str, *, class_name: str) -> str | None:
    """Return why a sign cannot be persisted, or ``None`` when valid."""
    normalised = " ".join(proposed.split()).strip()
    if not normalised:
        return "no sign was proposed"
    if len(normalised) > MAX_PURPOSE_CHARS:
        return f"sign is {len(normalised)} characters, past the {MAX_PURPOSE_CHARS} budget"
    if _REQUEST_HANDLE.search(normalised):
        return "sign carries a request-local document handle"
    # A useful routing sign must distinguish the folder from its siblings.
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
            "division is reconsidered once that number has doubled. Counted through the "
            "subtree because division moves direct documents into child folders."
        ),
    )
    redrawn_at_documents: int = Field(
        default=0,
        ge=0,
        description=(
            "Root only. How many documents the collection held when the whole-collection "
            "pass last drew the top of it. Kept separately from split_at_documents so "
            "root-level scheduling remains stable across individual divisions."
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
        if self.redrawn_at_documents:
            meta["redrawn_at_documents"] = self.redrawn_at_documents
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

        try:
            return cls(
                path=path,
                title=str(meta.get("title") or (path.name if path.name else "Vault root")),
                purpose=str(meta.get("purpose") or ""),
                holds=tuple(str(x) for x in meta.get("holds") or ()),
                answers=tuple(str(x) for x in meta.get("answers") or ()),
                managed=bool(meta.get("managed", True)),
                split_basis=str(meta.get("split_basis") or ""),
                split_question=str(meta.get("split_question") or ""),
                split_at_documents=int(meta.get("split_at_documents") or 0),
                redrawn_at_documents=int(meta.get("redrawn_at_documents") or 0),
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
