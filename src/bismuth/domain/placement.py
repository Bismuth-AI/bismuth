"""The outcome of asking "where does this document go?"."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field


class Verdict(StrEnum):
    PLACED = "placed"
    INBOX = "inbox"
    """The document could not be read, so there is nothing to file it by. Not for
    documents that are merely hard to sort -- those go to the root (SPEC.md 3.4)."""


class Placement(BaseModel):
    """The validated outcome of walking the existing folder tree."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    verdict: Verdict
    target: PurePosixPath | None = Field(
        default=None,
        description="Vault-relative destination folder. None when the verdict is INBOX.",
    )
    created_folder: bool = Field(
        default=False,
        description="True when this document caused its destination folder to be created.",
    )
    rationale: str = Field(
        default="",
        description=(
            "Short operational result kept for API and journal compatibility. The model "
            "does not generate placement prose."
        ),
    )
    folder_purpose: str = Field(
        default="",
        description=(
            "Stable routing sign supplied by the placement agent when it creates a folder. "
            "Empty for existing destinations."
        ),
    )
    companion_document_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Previously filed related documents at the same boundary parent that move "
            "with this placement. Catalog IDs, never model-facing handles."
        ),
    )

    @property
    def is_placed(self) -> bool:
        return self.verdict is Verdict.PLACED and self.target is not None

    @classmethod
    def to_inbox(cls, document_id: str, *, reason: str) -> Placement:
        return cls(
            document_id=document_id,
            verdict=Verdict.INBOX,
            rationale=reason,
        )
