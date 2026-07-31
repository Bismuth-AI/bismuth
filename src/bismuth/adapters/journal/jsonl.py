"""The journal as an append-only JSONL file; a reader takes the last status it sees for an id."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from bismuth.domain.errors import JournalCorruptError
from bismuth.domain.journal import EntryStatus, JournalEntry

JOURNAL_FILENAME = "journal.jsonl"


class JsonlJournal:
    """Append-only journal on disk."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)
        self._lock = threading.Lock()

    def append(self, entry: JournalEntry) -> None:
        self._write(entry)

    def update(self, entry: JournalEntry) -> None:
        self._write(entry)

    def _write(self, entry: JournalEntry) -> None:
        """Append one record and fsync before returning, so it is durable before the caller proceeds."""
        line = entry.model_dump_json() + "\n"
        with self._lock, open(self._path, "a", encoding="utf-8") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())

    def get(self, entry_id: str) -> JournalEntry | None:
        latest: JournalEntry | None = None
        for entry in self._read_all():
            if entry.id == entry_id:
                latest = entry
        return latest

    def iter_entries(
        self, *, limit: int | None = None, newest_first: bool = True
    ) -> Iterator[JournalEntry]:
        entries = list(self._latest_by_id().values())
        entries.sort(key=lambda e: e.created_at, reverse=newest_first)
        yield from entries[:limit] if limit is not None else entries

    def pending(self) -> list[JournalEntry]:
        return [e for e in self._latest_by_id().values() if e.status is EntryStatus.PENDING]

    def _latest_by_id(self) -> dict[str, JournalEntry]:
        latest: dict[str, JournalEntry] = {}
        for entry in self._read_all():
            latest[entry.id] = entry
        return latest

    def _read_all(self) -> Iterator[JournalEntry]:
        with open(self._path, encoding="utf-8") as file:
            lines = file.readlines()

        for number, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield JournalEntry.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as exc:
                if number == len(lines):
                    # A torn final line means a crash mid-append; the record never
                    # completed, so dropping it is correct, not lossy.
                    return
                raise JournalCorruptError(
                    f"{self._path}:{number} is unreadable ({exc}). Bismuth will not "
                    f"guess at a history it cannot read. Move this file aside to "
                    f"start a fresh journal -- your documents are untouched, but "
                    f"undo for past changes will be lost."
                ) from exc
