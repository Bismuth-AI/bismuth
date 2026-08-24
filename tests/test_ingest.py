"""Staging and preparing uploads."""

from __future__ import annotations

from pathlib import PurePosixPath

from bismuth.container import Bismuth
from tests.helpers import add


class TestIngestService:
    async def test_stage_writes_to_the_inbox(self, engine: Bismuth) -> None:
        rel = engine.ingest.stage(b"hello", "note.txt")
        assert rel == PurePosixPath("_inbox/note.txt")
        assert engine.vault.read_bytes(rel) == b"hello"

    async def test_prepare_reads_and_describes_a_document(self, engine: Bismuth) -> None:
        rel = engine.ingest.stage(b"hello", "note.txt")
        prepared = await engine.ingest.prepare(rel)
        assert prepared.card is not None
        assert prepared.extraction is not None
        assert prepared.source.filename == "note.txt"

    async def test_prepare_recognises_catalogued_content(self, engine: Bismuth) -> None:
        added = await add(engine, "original.txt", "same bytes")
        rel = engine.ingest.stage(b"same bytes", "copy.txt")
        prepared = await engine.ingest.prepare(rel)
        assert prepared.duplicate_of == added.document_id
