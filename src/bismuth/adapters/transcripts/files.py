"""Past conversations, one JSON file each, under ``.bismuth/conversations``.

A file per conversation rather than an append-only log: a transcript is rewritten on
every turn and deleted when a person says so, and both are one filesystem call here.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from bismuth.domain.transcript import Transcript, TranscriptSummary

TRANSCRIPTS_DIR = "conversations"


class FileTranscripts:
    """Chat history stored one conversation at a time."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir / TRANSCRIPTS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def save(self, transcript: Transcript) -> None:
        _atomic_write_text(self._path(transcript.id), transcript.model_dump_json(indent=2))

    def get(self, transcript_id: str) -> Transcript | None:
        return _read(self._path(transcript_id))

    def list(self, *, limit: int | None = None) -> list[TranscriptSummary]:
        found = [t for path in self._dir.glob("*.json") if (t := _read(path)) is not None]
        found.sort(key=lambda t: t.updated_at, reverse=True)
        kept = found[:limit] if limit is not None else found
        return [t.summary() for t in kept]

    def delete(self, transcript_id: str) -> None:
        self._path(transcript_id).unlink(missing_ok=True)

    def _path(self, transcript_id: str) -> Path:
        return self._dir / f"{_safe_id(transcript_id)}.json"


def _safe_id(value: str) -> str:
    """Reject identifiers that could alter a filesystem path."""
    if not value or not all(c.isalnum() or c in "-_" for c in value):
        raise ValueError(f"unsafe conversation id: {value!r}")
    return value


def _read(path: Path) -> Transcript | None:
    """A transcript, or nothing when the file is missing or no longer readable."""
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        return Transcript.model_validate(data)
    except (FileNotFoundError, json.JSONDecodeError, ValidationError):
        return None


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
