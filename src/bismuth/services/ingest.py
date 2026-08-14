"""Ingests a file: parse, describe, place, move, and write its sidecar and folder note."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import anyio

from bismuth.domain.charter import CHARTER_FILENAME
from bismuth.domain.document import DocumentCard, Extraction, SourceRef, sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.placement import Placement, Verdict
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_trace
from bismuth.ports.catalog import Catalog
from bismuth.ports.llm import CURRENT_DOCUMENT
from bismuth.ports.parser import ParserRegistry
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.cards import CardService
from bismuth.services.charters import ROOT_NOTE, CharterService
from bismuth.services.families import grounded_family_keys, key_sets_overlap
from bismuth.services.placement import PlacementService
from bismuth.services.sidecar import read_sidecar_meta, render_sidecar
from bismuth.services.subdivision import LibraryMaintenanceService
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Prepared:
    """A document read and catalogued, not yet filed.

    Produced without reading or writing the tree, which is what makes reading several
    documents at once safe while filing them stays one at a time.
    """

    rel: PurePosixPath
    source: SourceRef
    card: DocumentCard | None = None
    extraction: Extraction | None = None
    duplicate_of: str = ""
    """Set when these bytes were already in the catalog; then nothing was read."""


@dataclass(frozen=True, slots=True)
class _FamilyLocation:
    """Current colocated evidence for a grounded source-document family."""

    destination: PurePosixPath
    document_ids: tuple[str, ...]


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
        subdivision: LibraryMaintenanceService | None = None,
        extraction_max_chars: int = 200_000,
    ) -> None:
        self._subdivision = subdivision
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
        prepared = await self.prepare(rel, on_progress=on_progress)
        return await self.file(prepared, on_progress=on_progress)

    async def prepare(
        self, rel: PurePosixPath, *, on_progress: ProgressSink | None = None
    ) -> Prepared:
        """Read a document and work out what it is. Reads no folder and writes nothing.

        This half depends on the document and nothing else, so a caller may run it for
        several documents at once; :meth:`file` may not (see there). It is also where
        the run's time goes -- cataloguing is about two thirds of the calls a document
        costs and the great majority of its tokens.
        """
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
            logger.info("%s already ingested as %s; leaving it alone", rel, existing)
            return Prepared(rel=rel, source=source, duplicate_of=existing)

        say(Stage.PARSING, note=self._parser_name(rel))
        began = time.monotonic()
        extraction = await self._extract(rel)
        parse_ms = round((time.monotonic() - began) * 1000)
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
        log_trace(
            "document.read",
            filename=source.filename,
            parse_ms=parse_ms,
            card_ms=round((time.monotonic() - began) * 1000) - parse_ms,
            chars=len(extraction.text),
        )
        return Prepared(rel=rel, source=source, card=card, extraction=extraction)

    async def file(
        self, prepared: Prepared, *, on_progress: ProgressSink | None = None
    ) -> IngestResult:
        """Put a prepared document in the tree and let the tree react.

        **This half must run one document at a time.** Placement answers against the
        folder tree as it stands and subdivision then rewrites it, so two of these at
        once would decide against the same stale tree and race for the same folders.
        The order is load-bearing too: which tree a collection produces from a given
        order is a property the archive is measured on (SPEC.md 3.5).
        """
        rel, source = prepared.rel, prepared.source
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

        # Re-checked here and not only in prepare(): two copies read at the same time
        # both miss the catalog, and this is the half that runs alone.
        if existing := (prepared.duplicate_of or self._catalog.find_by_hash(source.sha256)):
            prior = self._catalog.load_placement(existing)
            # Placement records the decision at ingest time. Maintenance may have moved
            # the original since then, so report the sidecar's current filesystem path.
            fallback = (
                prior.target
                if prior and prior.is_placed and prior.target is not None
                else PurePosixPath(rel).parent
            )
            where = self._current_destination(existing, fallback=fallback)
            say(Stage.DUPLICATE, note=str(where) or "/")
            return IngestResult(
                document_id=existing,
                filename=source.filename,
                destination=where,
                placement=prior or Placement.to_inbox(existing, reason="already ingested"),
                duplicate=True,
            )

        card, extraction = prepared.card, prepared.extraction
        assert card is not None and extraction is not None  # only a duplicate lacks them

        # Where a document's time went, by stage. Without it a slow run is a wall of
        # calls with no shape: the first one measured spent 23 seconds on the document
        # and four minutes on one question put to the root afterwards.
        clock = _Clock()
        folders = self._charters.folder_views()
        say(Stage.PLACING, steps=len(folders))
        family = self._existing_family_location(card, source=source)
        # Root is provisional, not a semantic shelf. Locking every later family member
        # to it made an early undecided edition permanently immune to the placement
        # agent even after a suitable shelf existed. Re-evaluate a root family against
        # the current signs and move its already-filed companions atomically if it fits.
        if family is None or not family.destination.parts:
            placement = await self._placement.decide(
                document_id=source.document_id,
                card=card,
                folders=folders,
                existing_paths=frozenset(str(f) for f in self._vault.iter_folders() if f.parts),
            )
            if (
                family is not None
                and placement.target is not None
                and placement.target.parts
            ):
                placement = placement.model_copy(
                    update={"companion_document_ids": family.document_ids}
                )
                log_trace(
                    "place.family_promoted",
                    document_id=source.document_id,
                    title=card.title,
                    from_folder=".",
                    chose=str(placement.target),
                    companions=list(family.document_ids),
                )
        else:
            placement = Placement(
                document_id=source.document_id,
                verdict=Verdict.PLACED,
                target=family.destination,
                rationale="kept with an existing document family",
            )
            log_trace(
                "place.family_locked",
                document_id=source.document_id,
                title=card.title,
                chose=str(family.destination),
            )

        clock.mark("place")
        destination = placement.target if placement.is_placed else INBOX
        assert destination is not None
        # Placement details live in the trace; the progress line stays compact so one
        # completed document does not bury the rest of a batch.
        if not placement.is_placed:
            landed = "인박스 — 읽지 못했습니다"
        elif not destination.parts:
            landed = "루트 — 아직 나눌 구분이 없습니다"
        else:
            landed = f"{destination}{' (새 폴더)' if placement.created_folder else ''}"
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
        clock.mark("commit")

        say(Stage.NOTES)
        try:
            await self._reconcile_notes(placement)
        except Exception as exc:
            # The document and sidecar are already committed. A missing routing sign is
            # repairable metadata and must never turn a safely filed document into an API
            # failure or invite the caller to upload a duplicate.
            logger.exception("folder sign maintenance failed after filing %s", source.filename)
            log_trace(
                "charter.failed",
                filename=source.filename,
                document_id=source.document_id,
                folder=str(placement.target or ""),
                error=type(exc).__name__,
            )
        clock.mark("notes")

        # The other half of filing: this document may be the one that makes a
        # distinction visible in the folder it landed in (SPEC.md 3.4).
        structure_changed = False
        if self._subdivision is not None:
            try:
                divided = await self._subdivision.consider_with_ancestors(
                    destination, filename=source.filename, on_progress=on_progress
                )
                structure_changed = any(result.happened for result in divided)
                for result in divided:
                    say(Stage.DIVIDED, note=f"{result.folder or '/'} → {len(result.created)}개")
            except Exception as exc:
                # Filing is already committed and independently journalled. Maintenance
                # is allowed to fail without turning a safely filed, searchable document
                # into an apparent ingest failure. The failed maintenance transaction
                # rolls itself back; a later arrival can make another attempt.
                logger.exception("library maintenance failed after filing %s", source.filename)
                log_trace(
                    "maintenance.failed",
                    filename=source.filename,
                    error=f"{type(exc).__name__}: {exc}",
                )
        clock.mark("subdivide")

        # Avoid an O(collection) sidecar scan on ordinary ingests. It is needed only
        # when maintenance actually moved some part of the tree.
        final_destination = (
            self._current_destination(source.document_id, fallback=destination)
            if structure_changed
            else destination
        )
        if final_destination != destination:
            log_trace(
                "document.relocated",
                filename=source.filename,
                initial_destination=str(destination) or "/",
                final_destination=str(final_destination) or "/",
            )

        log_trace(
            "document.filed",
            filename=source.filename,
            destination=str(final_destination) or "/",
            total_ms=clock.total_ms,
            **clock.stages,
        )

        say(Stage.DONE, note=str(final_destination) or "/")
        return IngestResult(
            document_id=source.document_id,
            filename=source.filename,
            destination=final_destination,
            placement=placement,
            card=card,
        )

    def pending_inbox(self) -> list[PurePosixPath]:
        """Files in the inbox with no card yet -- including any dropped in by hand."""
        pending: list[PurePosixPath] = []
        for rel in self._vault.iter_files(INBOX, recursive=True):
            digest: str | None = None
            for attempt in range(3):
                try:
                    digest = SourceRef.hash_bytes(self._vault.read_bytes(rel))
                    break
                except BismuthError:
                    # `/api/status` can enumerate the inbox at the exact moment the
                    # batch worker commits a move out of it. A vanished entry is
                    # successful progress, not a corrupt vault.
                    if not self._vault.exists(rel):
                        break
                    raise
                except PermissionError as exc:
                    # Windows briefly denies a read while an upload/move handle is
                    # being closed. Status is observational and must not turn that
                    # transient race into a 500 response. Retry for at most 40 ms,
                    # then report the item as temporarily unavailable in diagnostics.
                    if not self._vault.exists(rel):
                        break
                    if attempt < 2:
                        time.sleep(0.02)
                        continue
                    logger.warning("status skipped temporarily locked inbox file %s", rel)
                    log_trace(
                        "inbox.status_read_deferred",
                        path=str(rel),
                        attempts=attempt + 1,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            if digest is None:
                continue
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

        reserved_targets: set[str] = set()
        for companion_id in placement.companion_document_ids:
            companion = self._current_document_path(companion_id)
            if companion is None or companion.parent == destination:
                continue
            companion_target = self._planned_unique_target(
                destination, companion.name, reserved=reserved_targets
            )
            operations.append(
                Operation(
                    kind=OperationKind.MOVE,
                    source=companion,
                    target=companion_target,
                    note=f"local companion for {source.filename}",
                )
            )
            reserved_targets.add(str(companion_target).casefold())
            old_sidecar = companion.parent / sidecar_name(companion.name)
            if self._vault.exists(old_sidecar):
                new_sidecar = companion_target.parent / sidecar_name(companion_target.name)
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=old_sidecar,
                        target=new_sidecar,
                        note="move companion sidecar",
                    )
                )
                reserved_targets.add(str(new_sidecar).casefold())

        if PurePosixPath(rel).parent == destination:
            final = rel
        else:
            final = self._planned_unique_target(
                destination, source.filename, reserved=reserved_targets
            )
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
            # The root is not a class and must not be described as one: its note is fixed
            # (see charters.ROOT_NOTE), or the first document becomes what the vault is
            # "about" for good.
            if not destination.parts:
                charter = ROOT_NOTE
            elif placement.created_folder and placement.folder_purpose:
                charter = self._charters.for_agent_created_folder(
                    destination, purpose=placement.folder_purpose
                )
            else:
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
        """Keep folder notes true after a document landed in one."""
        if not placement.is_placed or placement.target is None:
            return
        await self._redraw_notes([placement.target], reason="refresh folder notes")

    async def _redraw_notes(self, folders: list[PurePosixPath], *, reason: str) -> None:
        operations = await self._charters.refresh_operations(folders)
        if not operations:
            return
        self._transactor.execute(
            JournalEntry(reason=reason, operations=tuple(op for op, _ in operations)),
            payloads={op.target: payload for op, payload in operations},
        )

    def _current_destination(self, document_id: str, *, fallback: PurePosixPath) -> PurePosixPath:
        """Find a document's current shelf from its colocated sidecar.

        Catalog placements are historical decisions. Boundary maintenance is allowed
        to move any document afterwards, so using the original placement as current
        state produces stale API results and duplicate locations.
        """
        if document := self._current_document_path(document_id):
            return document.parent
        return fallback

    def _current_document_path(self, document_id: str) -> PurePosixPath | None:
        """Resolve a catalog ID through the sidecar colocated with the current file."""

        for document in self._vault.iter_files(PurePosixPath(), recursive=True):
            sidecar = document.parent / sidecar_name(document.name)
            if not self._vault.exists(sidecar):
                continue
            meta = read_sidecar_meta(self._vault.read_text(sidecar))
            if meta is not None and meta.get("document_id") == document_id:
                return document
        return None

    def _planned_unique_target(
        self,
        folder: PurePosixPath,
        filename: str,
        *,
        reserved: set[str],
    ) -> PurePosixPath:
        """Choose a collision-free target including moves planned in this transaction."""

        source_name = PurePosixPath(filename)
        stem, suffix = source_name.stem, source_name.suffix
        candidate = folder / source_name.name
        number = 2
        while self._vault.exists(candidate) or str(candidate).casefold() in reserved:
            candidate = folder / f"{stem} ({number}){suffix}"
            number += 1
        return candidate

    def _existing_family_location(
        self,
        card: DocumentCard,
        *,
        source: SourceRef,
    ) -> _FamilyLocation | None:
        """Reuse the current shelf of editions and subordinate instruments.

        The title must also be grounded at the start of each source filename. This
        prevents a generic or hallucinated card title from coupling unrelated files.
        When earlier editions are already split, their lowest common ancestor is the
        only shelf consistent with every precedent.
        """

        keys = grounded_family_keys(card, source.filename)
        if not keys:
            return None
        matches: list[tuple[str, PurePosixPath]] = []
        for document_id, existing_card in self._catalog.iter_cards():
            existing_source = self._catalog.load_source(document_id)
            if existing_source is None:
                continue
            existing_keys = grounded_family_keys(existing_card, existing_source.filename)
            if not key_sets_overlap(keys, existing_keys):
                continue
            prior = self._catalog.load_placement(document_id)
            fallback = (
                prior.target
                if prior is not None and prior.is_placed and prior.target is not None
                else PurePosixPath()
            )
            destination = self._current_destination(document_id, fallback=fallback)
            if destination.parts and not self._vault.is_dir(destination):
                continue
            matches.append((document_id, destination))
        if not matches:
            return None
        destination = _common_ancestor([item[1] for item in matches])
        # Root is a valid provisional family location. Keeping later instruments beside
        # the first one lets the multi-document structure harness move the whole family
        # together instead of letting placement split it before that evidence gathers.
        return _FamilyLocation(
            destination=destination,
            document_ids=tuple(document_id for document_id, _ in matches),
        )


class _Clock:
    """Wall time per stage of filing one document, for the trace.

    Marks rather than timers: each stage ends where the next begins, so the parts add
    up to the whole and a gap cannot hide between them.
    """

    def __init__(self) -> None:
        self._began = self._last = time.monotonic()
        self.stages: dict[str, int] = {}

    def mark(self, stage: str) -> None:
        now = time.monotonic()
        self.stages[f"{stage}_ms"] = round((now - self._last) * 1000)
        self._last = now

    @property
    def total_ms(self) -> int:
        return round((time.monotonic() - self._began) * 1000)


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


def _common_ancestor(paths: list[PurePosixPath]) -> PurePosixPath:
    """Deepest folder shared by every prior edition."""

    if not paths:
        return PurePosixPath()
    common = list(paths[0].parts)
    for path in paths[1:]:
        common = common[: _common_prefix_length(tuple(common), path.parts)]
    return PurePosixPath(*common)


def _common_prefix_length(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    length = 0
    for left_part, right_part in zip(left, right, strict=False):
        if left_part != right_part:
            break
        length += 1
    return length
