"""Ingests a file: parse, describe, place, move, and write its sidecar and folder note."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import anyio

from bismuth.domain.charter import CHARTER_FILENAME
from bismuth.domain.document import DocumentCard, Extraction, SourceRef, sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.placement import Placement
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.ports.catalog import Catalog
from bismuth.ports.parser import ParserRegistry
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.cards import CardService
from bismuth.services.charters import CharterService
from bismuth.services.placement import PlacementService
from bismuth.services.sidecar import render_sidecar
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What happened to one document."""

    document_id: str
    filename: str
    destination: PurePosixPath
    placement: Placement
    card: DocumentCard | None = None
    duplicate: bool = False
    """True when we had already ingested this content, under any filename."""


class IngestService:
    """Accepts a document and files it."""

    def __init__(
        self,
        *,
        vault: Vault,
        catalog: Catalog,
        parsers: ParserRegistry,
        cards: CardService,
        placement: PlacementService,
        charters: CharterService,
        transactor: Transactor,
        extraction_max_chars: int = 200_000,
    ) -> None:
        self._vault = vault
        self._catalog = catalog
        self._parsers = parsers
        self._cards = cards
        self._placement = placement
        self._charters = charters
        self._transactor = transactor
        self._max_chars = extraction_max_chars

    def stage(self, data: bytes, filename: str) -> PurePosixPath:
        """Put an uploaded file into the inbox, journalled."""
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

    async def process(
        self, rel: PurePosixPath, *, on_progress: ProgressSink | None = None
    ) -> IngestResult:
        """The whole loop for one document. Safe to call again on the same file."""
        source = await self._describe_source(rel)

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
            logger.info("%s already ingested as %s; leaving it alone", rel, existing)
            prior = self._catalog.load_placement(existing)
            # Report where the existing copy lives, not where this duplicate landed.
            where = (
                prior.target
                if prior and prior.is_placed and prior.target is not None
                else PurePosixPath(rel).parent
            )
            say(Stage.DUPLICATE, note=str(where) or "/")
            return IngestResult(
                document_id=existing,
                filename=source.filename,
                destination=where,
                placement=prior or Placement.to_inbox(existing, reason="already ingested"),
                duplicate=True,
            )

        say(Stage.PARSING, note=self._parser_name(rel))
        extraction = await self._extract(rel)
        # The window count rides on the reading events instead of being computed here:
        # counting means slicing the whole text, and the first reading event is next anyway.
        say(Stage.PARSED, note=_extent(extraction))

        card = await self._cards.describe(
            extraction,
            filename=source.filename,
            document_id=source.document_id,
            on_progress=on_progress,
        )
        say(Stage.CARDED, note=f"{card.title} ({card.doc_type})", found=card.topics)

        folders = self._charters.folder_views()
        say(Stage.PLACING, steps=len(folders))
        placement = await self._placement.decide(
            document_id=source.document_id,
            card=card,
            folders=folders,
            existing_paths=frozenset(str(f) for f in self._vault.iter_folders() if f.parts),
        )

        destination = placement.target if placement.is_placed else INBOX
        assert destination is not None
        # The full rationale is a paragraph and lives on the placement; a progress line
        # that long pushes every other step off the panel.
        if placement.is_placed:
            landed = f"{destination}{' (새 폴더)' if placement.created_folder else ''}"
        elif placement.suggested is not None:
            landed = f"인박스 — {placement.suggested} 를 제안했지만 확신 {placement.confidence:.0%}"
        else:
            landed = f"인박스 — 확신 {placement.confidence:.0%}"
        say(Stage.PLACED, note=landed)

        say(Stage.FILING)
        await self._commit(
            rel=rel,
            destination=destination,
            source=source,
            card=card,
            extraction=extraction,
            placement=placement,
        )

        self._catalog.save_card(source.document_id, card, source=source)
        self._catalog.save_placement(placement)

        say(Stage.NOTES)
        await self._reconcile_notes(placement)

        say(Stage.DONE, note=str(destination) or "/")
        return IngestResult(
            document_id=source.document_id,
            filename=source.filename,
            destination=destination,
            placement=placement,
            card=card,
        )

    def pending_inbox(self) -> list[PurePosixPath]:
        """Files in the inbox with no card yet -- including any dropped in by hand."""
        pending: list[PurePosixPath] = []
        for rel in self._vault.iter_files(INBOX, recursive=True):
            digest = SourceRef.hash_bytes(self._vault.read_bytes(rel))
            if self._catalog.find_by_hash(digest) is None:
                pending.append(rel)
        return pending

    def _parser_name(self, rel: PurePosixPath) -> str:
        """Which parser will read this, for the progress line. Unsupported types fail in _extract."""
        try:
            return self._parsers.for_path(Path(*rel.parts)).name
        except BismuthError:
            return rel.suffix.lstrip(".") or "알 수 없는 형식"

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
        # CPU-bound and slow: off the event loop, or a bulk import stalls the server.
        return await anyio.to_thread.run_sync(
            lambda: parser.parse(absolute, max_chars=self._max_chars)
        )

    async def _commit(
        self,
        *,
        rel: PurePosixPath,
        destination: PurePosixPath,
        source: SourceRef,
        card: DocumentCard,
        extraction: Extraction,
        placement: Placement,
    ) -> None:
        """Move the file, write its sidecar, and note a brand-new folder -- one batch."""
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}

        needs_note = (
            destination != INBOX
            and not self._vault.exists(destination / CHARTER_FILENAME)
            and self._charters.is_managed(destination)
        )
        if not self._vault.exists(destination):
            operations.append(Operation(kind=OperationKind.MKDIR, target=destination))

        if PurePosixPath(rel).parent == destination:
            final = rel
        else:
            final = self._vault.unique_target(destination, source.filename)
            operations.append(
                Operation(
                    kind=OperationKind.MOVE, source=rel, target=final, note=placement.rationale
                )
            )

        sidecar = final.parent / sidecar_name(final.name)
        operations.append(Operation(kind=OperationKind.WRITE, target=sidecar, note="sidecar"))
        payloads[sidecar] = render_sidecar(
            source=source, card=card, extraction=extraction, document_id=source.document_id
        ).encode("utf-8")

        if needs_note:
            charter = await self._charters.draft(destination, cards=[card], total_count=1)
            operation, payload = self._charters.write_operation(charter)
            operations.append(operation)
            payloads[operation.target] = payload

        self._transactor.execute(
            JournalEntry(
                reason=f"file {source.filename} -> {destination or '/'}",
                operations=tuple(operations),
                document_id=source.document_id,
            ),
            payloads=payloads,
        )

    async def _reconcile_notes(self, placement: Placement) -> None:
        """Keep folder notes true after a placement changed the tree."""
        if not placement.is_placed or placement.target is None:
            return
        dest = placement.target
        affected = _ancestors(dest) if placement.created_folder else [dest]
        operations = await self._charters.refresh_operations(affected)
        if not operations:
            return
        self._transactor.execute(
            JournalEntry(
                reason="refresh folder notes",
                operations=tuple(op for op, _ in operations),
            ),
            payloads={op.target: payload for op, payload in operations},
        )


def _extent(extraction: Extraction) -> str:
    """How much text came out, in the terms a person thinks in."""
    size = f"{len(extraction.text):,}자"
    if extraction.page_count:
        size = f"{extraction.page_count}쪽 · {size}"
    return f"{size}{' (추출 한도에서 잘림)' if extraction.truncated else ''}"


def _ancestors(path: PurePosixPath) -> list[PurePosixPath]:
    """Every folder above ``path``, nearest first, excluding the root."""
    result: list[PurePosixPath] = []
    parent = path.parent
    while parent.parts:
        result.append(parent)
        parent = parent.parent
    return result
