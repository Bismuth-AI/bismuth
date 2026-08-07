"""The ``_folder.md`` beside every folder: a one-line note on what it holds."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from bismuth.domain.errors import CharterError

#: Filename of a folder's note; sorts to the top of a listing.
CHARTER_FILENAME = "_folder.md"
CHARTER_SCHEMA_VERSION = 2

_GENERATED_BODY_NOTICE = (
    "<!-- 아래 본문은 이 파일의 frontmatter 에서 자동 생성됩니다. 고치려면 frontmatter 를 "
    "고치거나 Bismuth 화면에서 수정하세요. 본문을 직접 고치면 덮어써집니다. -->"
)


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
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def divided(self) -> bool:
        return bool(self.split_basis)

    @staticmethod
    def due_for_first_look(documents_now: int) -> bool:
        """Whether an undivided folder is worth asking about at this size.

        Powers of two, which is the doubling rule already used for review with no state
        to keep: ask at 2, 4, 8, 16. Asking on every arrival is thirteen chances to say
        yes about the same thirteen documents, and a judgement asked often enough
        eventually slips -- which is how a folder of unrelated papers acquired a sign
        reading "everything else".

        Scheduling, not judgement (SPEC.md 6.1): asking late costs a late division,
        never a wrong one.
        """
        return documents_now >= 2 and documents_now & (documents_now - 1) == 0

    def due_for_review(self, documents_now: int) -> bool:
        """Whether the evidence has doubled since this folder was divided.

        ``documents_now`` is the count through the subtree, matching what was recorded.

        Scheduling, not judgement: asking late costs a late fix, never a wrong tree
        (SPEC.md 6.1). The ratio is to the folder's own history, so nothing here is
        tuned to a corpus.
        """
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
            "holds": list(self.holds),
            "answers": list(self.answers),
        }
        if self.divided:
            meta["split_basis"] = self.split_basis
            meta["split_at_documents"] = self.split_at_documents
        front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
        return f"---\n{front}---\n\n{self._render_body()}\n"

    def _render_body(self) -> str:
        lines: list[str] = [_GENERATED_BODY_NOTICE, "", f"# {self.title}", "", self.purpose, ""]
        if self.holds:
            lines += ["## 여기에 들어오는 것", ""]
            lines += [f"- {item}" for item in self.holds]
            lines += [""]
        if self.answers:
            lines += ["## 여기서 답할 수 있는 질문", ""]
            lines += [f"- {question}" for question in self.answers]
            lines += [""]
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
                split_at_documents=int(meta.get("split_at_documents") or 0),
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
