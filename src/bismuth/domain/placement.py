"""The outcome of asking "where does this document go?"."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field


class Verdict(StrEnum):
    PLACED = "placed"
    INBOX = "inbox"
    """The document waits in the inbox instead of being guessed at: unreadable, or the
    model was not sure enough. Expected to be uncommon -- if a vault is filling its inbox
    the placement prompt is the thing to look at, not the threshold."""


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
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "What the model reported, kept whatever the verdict. Parking a document used "
            "to zero this and write the number into the Korean rationale instead, which "
            "left no way to ask how close a parked document came without parsing prose."
        ),
    )
    suggested: PurePosixPath | None = Field(
        default=None,
        description=(
            "Where the model would have put it, when the verdict is INBOX and it named a "
            "folder. Thrown away previously, which left the user re-deciding from nothing."
        ),
    )
    rationale: str = Field(
        default="",
        description="Why this folder, in a sentence a person can check against the document.",
    )

    @property
    def is_placed(self) -> bool:
        return self.verdict is Verdict.PLACED and self.target is not None

    @classmethod
    def to_inbox(
        cls,
        document_id: str,
        *,
        reason: str,
        confidence: float = 0.0,
        suggested: PurePosixPath | None = None,
    ) -> Placement:
        return cls(
            document_id=document_id,
            verdict=Verdict.INBOX,
            rationale=reason,
            confidence=confidence,
            suggested=suggested,
        )
