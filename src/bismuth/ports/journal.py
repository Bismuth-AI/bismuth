"""The journal boundary: an append-only log with no delete or rewrite."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from bismuth.domain.journal import JournalEntry


@runtime_checkable
class JournalStore(Protocol):
    """Durable, append-only history of every change to a vault."""

    def append(self, entry: JournalEntry) -> None:
        """Persist a new entry. Must be durable on return (fsync, not buffered)."""
        ...

    def update(self, entry: JournalEntry) -> None:
        """Record a status transition; implementations append rather than mutate."""
        ...

    def get(self, entry_id: str) -> JournalEntry | None: ...

    def iter_entries(
        self, *, limit: int | None = None, newest_first: bool = True
    ) -> Iterator[JournalEntry]:
        """Walk history. Backs ``bismuth log`` and the activity feed."""
        ...

    def pending(self) -> list[JournalEntry]:
        """Entries started but never finished; read at startup as the recovery worklist."""
        ...
