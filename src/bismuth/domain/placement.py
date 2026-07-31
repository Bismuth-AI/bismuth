"""The outcome of asking "where does this document go?"."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field


class Verdict(StrEnum):
    PLACED = "placed"
    INBOX = "inbox"
    """Bismuth could not read or make sense of the document, so it sits in the
    inbox rather than being guessed at. Rare -- the normal outcome is a folder."""


class Placement(BaseModel):
    """Where a document goes, and the reasoning a human can audit."""

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
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(
        default="",
        description="Why this folder, in a sentence a person can check against the document.",
    )

    @property
    def is_placed(self) -> bool:
        return self.verdict is Verdict.PLACED and self.target is not None

    @classmethod
    def to_inbox(cls, document_id: str, *, reason: str) -> Placement:
        return cls(document_id=document_id, verdict=Verdict.INBOX, rationale=reason)
