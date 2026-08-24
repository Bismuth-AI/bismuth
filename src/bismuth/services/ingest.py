"""Stage uploads and prepare them for the filing service."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import anyio

from bismuth.domain.document import DocumentCard, Extraction, SourceRef
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_trace
from bismuth.ports.catalog import Catalog
from bismuth.ports.llm import CURRENT_DOCUMENT
from bismuth.ports.parser import ParserRegistry
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.cards import CardService
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Prepared:
    """A document that has been read but not filed."""

    rel: PurePosixPath
    source: SourceRef
    card: DocumentCard | None = None
    extraction: Extraction | None = None
    duplicate_of: str = ""
    preserve_sidecar: bool = False
    preserve_catalog: bool = False


class IngestService:
    """Persist uploads safely, then parse and describe them."""

    def __init__(
        self,
        *,
        vault: Vault,
        catalog: Catalog,
        parsers: ParserRegistry,
        cards: CardService,
        transactor: Transactor,
        extraction_max_chars: int = 200_000,
    ) -> None:
        self._vault = vault
        self._catalog = catalog
        self._parsers = parsers
        self._cards = cards
        self._transactor = transactor
        self._max_chars = extraction_max_chars

    def stage(self, data: bytes, filename: str) -> PurePosixPath:
        """Write an upload to the inbox in one journalled operation."""
        target = self._vault.unique_target(INBOX, filename)
        self._transactor.execute(
            JournalEntry(
                actor=Actor.USER,
                reason=f"received {filename}",
                operations=(Operation(kind=OperationKind.WRITE, target=target),),
            ),
            payloads={target: data},
        )
        return PurePosixPath(target)

    async def prepare(
        self, rel: PurePosixPath, *, on_progress: ProgressSink | None = None
    ) -> Prepared:
        """Parse and describe a staged document without changing the folder tree."""
        source = await self._describe_source(rel)
        CURRENT_DOCUMENT.set(source.document_id)

        def say(stage: Stage, **fields: object) -> None:
            report(
                on_progress,
                Progress(
                    stage=stage,
                    filename=source.filename,
                    document_id=source.document_id,
                    **fields,  # type: ignore[arg-type]
                ),
            )

        say(Stage.RECEIVED)
        if existing := self._catalog.find_by_hash(source.sha256):
            logger.info("%s already ingested as %s", rel, existing)
            say(Stage.DUPLICATE, note=existing)
            return Prepared(rel=rel, source=source, duplicate_of=existing)

        say(Stage.PARSING, note=self._parser_name(rel))
        started = time.monotonic()
        extraction = await self._extract(rel)
        parse_ms = round((time.monotonic() - started) * 1000)
        say(Stage.PARSED, note=_extent(extraction))

        card = await self._cards.describe(
            extraction,
            filename=source.filename,
            document_id=source.document_id,
            on_progress=on_progress,
        )
        say(Stage.CARDED, note=f"{card.title} ({card.doc_type})", found=card.topics)
        log_trace(
            "document.read",
            filename=source.filename,
            parse_ms=parse_ms,
            card_ms=round((time.monotonic() - started) * 1000) - parse_ms,
            chars=len(extraction.text),
        )
        return Prepared(rel=rel, source=source, card=card, extraction=extraction)

    def pending_inbox(self) -> list[PurePosixPath]:
        """Return inbox files that have not been catalogued."""
        pending: list[PurePosixPath] = []
        for rel in self._vault.iter_files(INBOX, recursive=True):
            try:
                digest = SourceRef.hash_bytes(self._vault.read_bytes(rel))
            except BismuthError:
                if not self._vault.exists(rel):
                    continue
                raise
            if self._catalog.find_by_hash(digest) is None:
                pending.append(rel)
        return pending

    def discard_duplicate(self, rel: PurePosixPath) -> None:
        """Remove a staged copy whose content is already catalogued."""
        if not self._vault.exists(rel):
            return
        self._transactor.execute(
            JournalEntry(
                reason=f"discard duplicate {rel.name}",
                operations=(Operation(kind=OperationKind.REMOVE, target=rel),),
            )
        )

    def _parser_name(self, rel: PurePosixPath) -> str:
        try:
            return self._parsers.for_path(Path(*rel.parts)).name
        except BismuthError:
            return rel.suffix.lstrip(".") or "unknown format"

    async def _describe_source(self, rel: PurePosixPath) -> SourceRef:
        absolute = Path(self._vault.root) / Path(*rel.parts)
        stat = await anyio.to_thread.run_sync(absolute.stat)
        data = await anyio.to_thread.run_sync(absolute.read_bytes)
        return SourceRef(
            path=absolute,
            filename=absolute.name,
            size_bytes=stat.st_size,
            sha256=SourceRef.hash_bytes(data),
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    async def _extract(self, rel: PurePosixPath) -> Extraction:
        absolute = Path(self._vault.root) / Path(*rel.parts)
        parser = self._parsers.for_path(absolute)
        return await anyio.to_thread.run_sync(
            lambda: parser.parse(absolute, max_chars=self._max_chars)
        )


def _extent(extraction: Extraction) -> str:
    size = f"{len(extraction.text):,} characters"
    if extraction.page_count:
        size = f"{extraction.page_count} pages, {size}"
    return f"{size}{' (truncated)' if extraction.truncated else ''}"
