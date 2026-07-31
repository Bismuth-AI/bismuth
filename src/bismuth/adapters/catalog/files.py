"""Derived state as plain files under ``.bismuth/``; fully reconstructible from the vault, so nothing here is irreplaceable."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from bismuth.domain.document import DocumentCard, SourceRef
from bismuth.domain.placement import Placement


class FileCatalog:
    """Cards and placements, one file at a time."""

    def __init__(self, state_dir: Path) -> None:
        self._cards = state_dir / "cards"
        self._placements = state_dir / "placements"
        for directory in (self._cards, self._placements):
            directory.mkdir(parents=True, exist_ok=True)

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
        (self._placements / f"{_safe_id(document_id)}.json").unlink(missing_ok=True)

    def _card_path(self, document_id: str) -> Path:
        return self._cards / f"{_safe_id(document_id)}.json"

    def save_placement(self, placement: Placement) -> None:
        _write_json(self._placements / f"{_safe_id(placement.document_id)}.json", placement)

    def load_placement(self, document_id: str) -> Placement | None:
        data = _read_json(self._placements / f"{_safe_id(document_id)}.json")
        if data is None:
            return None
        try:
            return Placement.model_validate(data)
        except ValidationError:
            return None


def _safe_id(value: str) -> str:
    """Refuse ids that could become a path. Document ids are hex, so this never fires."""
    if not value or not all(c.isalnum() or c in "-_" for c in value):
        raise ValueError(f"unsafe catalog id: {value!r}")
    return value


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json(path: Path, model: BaseModel) -> None:
    _atomic_write_text(path, model.model_dump_json(indent=2))


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
