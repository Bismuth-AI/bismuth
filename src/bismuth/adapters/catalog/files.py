"""File-backed document cards under ``.bismuth/``."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from bismuth.domain.document import DocumentCard, SourceRef


class FileCatalog:
    """Document cards stored one file at a time."""

    def __init__(self, state_dir: Path) -> None:
        self._cards = state_dir / "cards"
        self._cards.mkdir(parents=True, exist_ok=True)

    def load_card(self, document_id: str) -> DocumentCard | None:
        data = _read_json(self._card_path(document_id))
        if data is None:
            return None
        try:
            return DocumentCard.model_validate(data["card"])
        except (KeyError, ValidationError):
            return None

    def save_card(self, document_id: str, card: DocumentCard, *, source: SourceRef) -> None:
        _write_json_raw(
            self._card_path(document_id),
            {
                "document_id": document_id,
                "source": json.loads(source.model_dump_json()),
                "card": json.loads(card.model_dump_json()),
            },
        )

    def load_source(self, document_id: str) -> SourceRef | None:
        data = _read_json(self._card_path(document_id))
        if data is None:
            return None
        try:
            return SourceRef.model_validate(data["source"])
        except (KeyError, ValidationError):
            return None

    def iter_cards(self) -> Iterator[tuple[str, DocumentCard]]:
        for path in sorted(self._cards.glob("*.json")):
            data = _read_json(path)
            if data is None:
                continue
            try:
                yield data["document_id"], DocumentCard.model_validate(data["card"])
            except (KeyError, ValidationError):
                continue

    def card_count(self) -> int:
        return sum(1 for _ in self._cards.glob("*.json"))

    def find_by_hash(self, sha256: str) -> str | None:
        """Whether we have already ingested this content, by its hash-derived id."""
        document_id = sha256[:16]
        return document_id if self._card_path(document_id).exists() else None

    def forget(self, document_id: str) -> None:
        self._card_path(document_id).unlink(missing_ok=True)

    def _card_path(self, document_id: str) -> Path:
        return self._cards / f"{_safe_id(document_id)}.json"


def _safe_id(value: str) -> str:
    """Reject identifiers that could alter a filesystem path."""
    if not value or not all(c.isalnum() or c in "-_" for c in value):
        raise ValueError(f"unsafe catalog id: {value!r}")
    return value


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json_raw(path: Path, data: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


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
