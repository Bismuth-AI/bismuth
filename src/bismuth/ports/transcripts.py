"""The transcript boundary: past conversations, listed, reopened, and deleted."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bismuth.domain.transcript import Transcript, TranscriptSummary


@runtime_checkable
class TranscriptStore(Protocol):
    """Durable chat history for one vault. Unlike the journal, entries can be removed."""

    def save(self, transcript: Transcript) -> None:
        """Write the whole conversation, replacing the stored copy."""
        ...

    def get(self, transcript_id: str) -> Transcript | None: ...

    def list(self, *, limit: int | None = None) -> list[TranscriptSummary]:
        """Summaries, most recently answered first."""
        ...

    def delete(self, transcript_id: str) -> None:
        """Forget one conversation. Deleting what is not there is not an error."""
        ...
