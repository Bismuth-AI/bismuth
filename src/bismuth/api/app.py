"""The HTTP API and the window onto a vault. Localhost, unauthenticated -- a local tool for local documents."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, cast
from urllib.parse import quote

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from bismuth import __version__
from bismuth.adapters.llm import (
    list_models,
    litellm_adapter,
    suggest_model,
    supports_response_schema,
)
from bismuth.api.maintenance import MaintenanceState
from bismuth.api.maintenance import load as load_maintenance
from bismuth.api.maintenance import recover_interrupted as recover_interrupted_maintenance
from bismuth.api.maintenance import save as save_maintenance
from bismuth.api.progress import ProgressBus, stream
from bismuth.config import PROVIDERS, Settings, load_env_file, provider, save_user_config
from bismuth.container import Bismuth, build
from bismuth.domain.document import sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.progress import Progress, Stage
from bismuth.logging_setup import configure_logging
from bismuth.ports.llm import Spend
from bismuth.ports.vault import INBOX
from bismuth.services.agent import DEFAULT_ORGANIZE_INSTRUCTION
from bismuth.services.ingest import IngestResult
from bismuth.services.maintenance_windows import next_window, window_ready
from bismuth.services.sidecar import read_sidecar_meta

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"


def _preload(engine: Bismuth) -> None:
    """Pull every deferred import in before the server accepts a request.

    Two things import late: LiteLLM, to beat python-dotenv's upward ``.env`` scan, and
    the document parsers, which are an optional extra. Both deferrals are about *when*,
    not *whether* -- a server that pays a multi-second import inside the first upload,
    or discovers a missing parser there, reported ready before it was.

    A missing optional parser is logged, not fatal: a minimal install is supported.
    """
    litellm_adapter.preload()
    if unavailable := engine.parsers.warm():
        logger.warning(
            "%d parser(s) unavailable; those formats will be refused: %s",
            len(unavailable),
            ", ".join(sorted(unavailable)),
        )
    logger.info("preloaded: litellm, %d parser(s)", len(engine.parsers.supported_extensions()))


def get_engine(request: Request) -> Bismuth:
    """The engine for this app, from app state. Must stay module-level or FastAPI treats it as a query param (422)."""
    engine: Bismuth = request.app.state.engine
    return engine


Engine = Annotated[Bismuth, Depends(get_engine)]


def create_app(settings: Settings, *, verbose: bool = False) -> FastAPI:
    load_env_file()
    engine = build(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Logging is set up here and not in create_app because uvicorn configures its
        # own with dictConfig, which closes every handler that already existed. Ours
        # stayed attached to their loggers and stayed enabled, so nothing raised and
        # nothing was written: a server that ingested thirty-three documents left two
        # lines in bismuth.log and empty trace and llm files. Startup runs after that,
        # so what is opened here survives.
        configure_logging(verbose=verbose)
        _preload(engine)
        if recovered := engine.recover():
            logger.warning("rolled back %d interrupted change(s) from a previous run", recovered)
        recover_interrupted_maintenance(engine.vault.root)
        try:
            yield
        finally:
            tasks: set[asyncio.Task[None]] = getattr(app.state, "batch_tasks", set())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            maintenance_task: asyncio.Task[None] | None = getattr(
                app.state, "maintenance_task", None
            )
            if maintenance_task is not None and not maintenance_task.done():
                maintenance_task.cancel()
                await asyncio.gather(maintenance_task, return_exceptions=True)
            await litellm_adapter.close_clients()

    app = FastAPI(title="Bismuth", version=__version__, lifespan=lifespan)
    app.state.engine = engine
    app.state.settings = settings
    app.state.progress = ProgressBus()
    app.state.ingest_lock = asyncio.Lock()
    app.state.batches = {}
    app.state.batch_tasks = set()
    app.state.maintenance_task = None

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        """Return the real error instead of a bare 500 -- no attacker on localhost to hide it from."""
        logger.exception("unhandled error")
        return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})

    @app.get("/api/setup", response_model=SetupStateOut)
    def setup_state() -> SetupStateOut:
        current = app.state.settings
        return SetupStateOut(
            configured=current.is_configured,
            providers=[p.model_dump(mode="json") for p in PROVIDERS],
            provider_id=current.provider_id,
            api_base=current.api_base,
            model=current.model,
            vault_path=str(current.vault_path),
            api_headers=current.api_headers,
            api_body=current.api_body,
            native_schema=current.native_schema,
            api_key_tail=f"…{current.api_key[-4:]}" if current.api_key else "",
        )

    @app.post("/api/setup/check", response_model=ProviderCheckOut)
    async def setup_check(body: ProviderCheckIn) -> ProviderCheckOut:
        """Ask the provider what this key can reach. Listing the catalogue is the check."""
        chosen = provider(body.provider_id)
        if chosen is None:
            raise HTTPException(400, f"알 수 없는 프로바이더: {body.provider_id}")
        key = body.api_key or (app.state.settings.api_key if body.reuse_saved_key else "")
        check = await anyio.to_thread.run_sync(
            lambda: list_models(
                chosen.id,
                api_key=key,
                api_base=body.api_base or chosen.default_api_base,
                headers=body.api_headers,
            )
        )
        return ProviderCheckOut(
            ok=check.ok,
            error=check.error,
            models=list(check.models),
            suggested_model=suggest_model(check.models),
        )

    @app.post("/api/setup", response_model=SetupStateOut)
    async def setup_save(body: SetupIn) -> SetupStateOut:
        """Persist the wizard's answers and rebuild the engine around them, in place."""
        chosen = provider(body.provider_id)
        if chosen is None:
            raise HTTPException(400, f"알 수 없는 프로바이더: {body.provider_id}")
        key = body.api_key or (app.state.settings.api_key if body.reuse_saved_key else "")
        if chosen.needs_key and not key:
            raise HTTPException(400, f"{chosen.label} 에는 키가 필요합니다.")

        # Asked of the endpoint, once, rather than of LiteLLM's table of known models:
        # a self-hosted server is always absent from that table, so every structured
        # call fell back to describing the schema in the prompt and repairing what came
        # back. Only for the compatible option; the hosted two are in the table.
        native: bool | None = None
        if chosen.id == "custom":
            native = await anyio.to_thread.run_sync(
                lambda: supports_response_schema(
                    api_base=body.api_base or chosen.default_api_base or "",
                    model=body.model,
                    api_key=key,
                    headers=body.api_headers,
                )
            )
            logger.info("%s constrains decoding to a schema: %s", body.api_base, native)

        updated = Settings(
            vault_path=Path(body.vault_path).expanduser(),
            provider_id=chosen.id,
            api_key=key,
            api_base=body.api_base or chosen.default_api_base,
            api_headers=body.api_headers,
            api_body=body.api_body,
            native_schema=native,
            model=body.model,
        )
        if not updated.is_configured:
            raise HTTPException(400, "모델을 골라 주세요.")

        save_user_config(updated)
        app.state.settings = updated
        app.state.engine = build(updated)
        # The wizard swaps the engine in a live process; the replacement has to be as
        # warm as the one created at startup, or the first upload after setup pays for it.
        _preload(app.state.engine)
        logger.info("configuration updated: %s", updated.redacted())
        return setup_state()

    @app.get("/api/maintenance", response_model=MaintenanceOut)
    def maintenance_state(engine: Engine) -> MaintenanceOut:
        """Return the durable checkpoint for the current vault's structure pass."""
        return MaintenanceOut.of(load_maintenance(engine.vault.root))

    @app.post("/api/maintenance/retry", response_model=MaintenanceOut, status_code=202)
    async def maintenance_retry() -> MaintenanceOut:
        """Retry structure planning with the current model, without re-ingesting documents."""
        engine: Bismuth = app.state.engine
        current = load_maintenance(engine.vault.root)
        task: asyncio.Task[None] | None = app.state.maintenance_task
        if (task is not None and not task.done()) or current.status in {"pending", "running"}:
            raise HTTPException(409, "Library maintenance is already running.")

        had_backlog = bool(current.pending_document_ids)
        backlog = current.pending_document_ids
        if not backlog:
            # A manual structure action on a pre-checkpoint vault starts from the
            # existing card catalogue. _drain_maintenance will still process it in
            # bounded windows rather than presenting the whole collection at once.
            backlog = [document_id for document_id, _ in engine.catalog.iter_cards()]
        pending = current.model_copy(
            update={
                "status": "pending",
                "source": "manual-retry",
                "error": "",
                "summary": "",
                "moved": current.moved if had_backlog else 0,
                "applied": current.applied if had_backlog else False,
                "pending_document_ids": backlog,
                "completed_windows": current.completed_windows if had_backlog else 0,
                "current_window_documents": 0,
                "finished_at": None,
            }
        )
        save_maintenance(engine.vault.root, pending)

        async def resume() -> None:
            async with app.state.ingest_lock:
                # Resolve the engine only after obtaining the lock. If setup replaced the
                # model while this request was queued, the retry uses that replacement.
                await _drain_maintenance(app.state.engine, source="manual-retry")

        task = asyncio.create_task(resume(), name="bismuth-maintenance-retry")
        app.state.maintenance_task = task

        def clear(completed: asyncio.Task[None]) -> None:
            if app.state.maintenance_task is completed:
                app.state.maintenance_task = None

        task.add_done_callback(clear)
        return MaintenanceOut.of(pending)

    @app.get("/api/status", response_model=StatusOut)
    def status(engine: Engine) -> StatusOut:
        # Disk, not the card cache -- the two can diverge (e.g. after an undone delete).
        inbox_count = engine.vault.count_files(INBOX, recursive=True)
        placed = sum(
            engine.vault.count_files(f, recursive=False)
            for f in engine.vault.iter_folders()
            # Root is a real shelf: young libraries deliberately keep documents there
            # until a useful distinction emerges. Exclude only the inbox, not root.
            if not f.parts or f.parts[0] != INBOX.parts[0]
        )
        return StatusOut(
            configured=app.state.settings.is_configured,
            vault=str(engine.vault.root),
            documents=placed + inbox_count,
            # Excludes inbox, or a fresh vault would report "1 folder" already.
            folders=sum(
                1 for f in engine.vault.iter_folders() if f.parts and f.parts[0] != INBOX.parts[0]
            ),
            inbox=engine.vault.count_files(INBOX, recursive=True),
            unprocessed=len(engine.ingest.pending_inbox()),
            placed=placed,
            unplaced=inbox_count,
            runs_locally=app.state.settings.runs_locally,
            supported_formats=sorted(engine.parsers.supported_extensions()),
            spend=engine.ledger.total(),
        )

    @app.get("/api/tree", response_model=list[FolderOut])
    def tree(engine: Engine) -> list[FolderOut]:
        folders: list[FolderOut] = []
        for folder in engine.vault.iter_folders():
            purpose = ""
            try:
                if charter := engine.charters.load(folder):
                    purpose = charter.purpose
            except BismuthError:
                purpose = "(폴더 노트를 읽을 수 없습니다)"
            folders.append(
                FolderOut(
                    path=str(folder),
                    name=folder.name or "/",
                    depth=len(folder.parts),
                    files=engine.vault.count_files(folder),
                    purpose=purpose,
                )
            )
        return folders

    @app.get("/api/folder", response_model=FolderDetailOut)
    def folder(path: str, engine: Engine) -> FolderDetailOut:
        rel = PurePosixPath(path) if path not in ("", "/") else PurePosixPath()
        if not engine.vault.is_dir(rel):
            raise HTTPException(404, f"그런 폴더가 없습니다: {path}")
        charter = None
        try:
            charter = engine.charters.load(rel)
        except BismuthError as exc:
            logger.warning("unreadable folder note at %s: %s", rel, exc)
        return FolderDetailOut(
            path=str(rel),
            charter=charter.model_dump(mode="json") if charter else None,
            documents=[DocumentOut.of(engine, file) for file in engine.vault.iter_files(rel)],
        )

    @app.get("/api/file")
    def open_file(path: str, engine: Engine) -> Response:
        """Serve one document's raw bytes so the browser can open or preview it."""
        rel = PurePosixPath(path)
        try:
            if engine.vault.is_dir(rel):  # resolve() inside is_dir/read_bytes refuses escapes
                raise HTTPException(400, "폴더는 열 수 없습니다.")
            data = engine.vault.read_bytes(rel)
        except BismuthError as exc:
            raise HTTPException(404, str(exc)) from exc
        media = mimetypes.guess_type(rel.name)[0] or "application/octet-stream"
        disposition = f"inline; filename*=UTF-8''{quote(rel.name)}"
        return Response(data, media_type=media, headers={"content-disposition": disposition})

    @app.post("/api/documents", response_model=list[IngestOut])
    async def upload(files: list[UploadFile], engine: Engine) -> list[IngestOut]:
        """Accept files and file them. Each is journalled into the inbox before anything clever."""
        results: list[IngestOut] = []
        maintenance_source = f"upload:{uuid.uuid4().hex[:8]}"
        arrivals = 0
        blocked = load_maintenance(engine.vault.root).status == "failed"
        async with app.state.ingest_lock:
            for upload_file in files:
                data = await upload_file.read()
                name = Path(upload_file.filename or "untitled").name
                rel = engine.ingest.stage(data, name)
                result = await _process(engine, rel)
                results.append(result)
                if _is_new_arrival(result):
                    arrivals += 1
                    state = _enqueue_documents(
                        engine, [result.document_id], source=maintenance_source
                    )
                    if not blocked and window_ready(engine.catalog, state.pending_document_ids):
                        state = await _drain_maintenance(
                            engine, source=maintenance_source, max_windows=1
                        )
                        blocked = state.status == "failed"
            state = load_maintenance(engine.vault.root)
            if not blocked and _tail_due(engine, arrivals, state):
                await _drain_maintenance(engine, source=maintenance_source)
        return results

    async def run_batch(batch_id: str, staged: list[PurePosixPath], engine: Bismuth) -> None:
        """Process already-safe inbox files independently of the browser connection."""
        batch: BatchOut = app.state.batches[batch_id]
        batch.status = "queued"
        try:
            async with app.state.ingest_lock:
                batch.status = "running"
                arrivals = 0
                blocked = load_maintenance(engine.vault.root).status == "failed"
                for rel in staged:
                    batch.current = rel.name
                    batch.current_stage = "received"
                    batch.current_label = "읽기 준비 중"

                    def report(progress: Progress) -> None:
                        batch.current = progress.filename
                        batch.current_stage = progress.stage.value
                        batch.current_label = progress.label()
                        app.state.progress.publish(progress)

                    try:
                        result = await _process(engine, rel, on_progress=report)
                    except Exception as exc:
                        logger.exception("batch %s failed while processing %s", batch_id, rel)
                        report(Progress(stage=Stage.FAILED, filename=rel.name, note=str(exc)))
                        result = IngestOut(filename=rel.name, ok=False, reason=str(exc))

                    batch.completed += 1
                    if not result.ok:
                        batch.failed += 1
                    elif result.duplicate:
                        batch.duplicate += 1
                    elif not result.placed:
                        batch.inbox += 1
                    if _is_new_arrival(result):
                        arrivals += 1
                        state = _enqueue_documents(
                            engine, [result.document_id], source=f"batch:{batch_id}"
                        )
                        if not blocked and window_ready(
                            engine.catalog, state.pending_document_ids
                        ):
                            batch.current = ""
                            batch.current_stage = "maintenance"
                            batch.current_label = (
                                f"구조 정리 중 — {state.completed_windows + 1}번째 묶음"
                            )
                            state = await _drain_maintenance(
                                engine,
                                source=f"batch:{batch_id}",
                                max_windows=1,
                            )
                            blocked = state.status == "failed"
                state = load_maintenance(engine.vault.root)
                if not blocked and _tail_due(engine, arrivals, state):
                    batch.current = ""
                    batch.current_stage = "maintenance"
                    batch.current_label = "남은 도착 문서 구조 정리 중"
                    await _drain_maintenance(engine, source=f"batch:{batch_id}")
            batch.status = "done"
        except asyncio.CancelledError:
            batch.status = "interrupted"
            raise
        except Exception:
            batch.status = "failed"
            logger.exception("batch %s stopped unexpectedly", batch_id)
        finally:
            batch.finished_at = time.time()
            if batch.status == "done":
                batch.current = ""
                batch.current_stage = "done"
                batch.current_label = "모든 파일 정리 완료"

    @app.post("/api/batches", response_model=BatchOut, status_code=202)
    async def create_batch(files: list[UploadFile], engine: Engine) -> BatchOut:
        """Stage a whole selection, then let the server finish it after a page refresh."""
        if not files:
            raise HTTPException(400, "올릴 파일이 없습니다.")
        staged: list[PurePosixPath] = []
        async with app.state.ingest_lock:
            for upload_file in files:
                data = await upload_file.read()
                name = Path(upload_file.filename or "untitled").name
                staged.append(engine.ingest.stage(data, name))

        batch_id = uuid.uuid4().hex[:12]
        batch = BatchOut(
            id=batch_id,
            total=len(staged),
            filenames=[rel.name for rel in staged],
            created_at=time.time(),
        )
        app.state.batches[batch_id] = batch
        task = asyncio.create_task(
            run_batch(batch_id, staged, engine), name=f"bismuth-batch-{batch_id}"
        )
        app.state.batch_tasks.add(task)
        task.add_done_callback(app.state.batch_tasks.discard)
        return batch.model_copy(deep=True)

    @app.get("/api/batches", response_model=list[BatchOut])
    def list_batches() -> list[BatchOut]:
        """Active and recent work, used to rebuild progress cards after a reload."""
        cutoff = time.time() - 3600
        expired = [
            batch_id
            for batch_id, batch in app.state.batches.items()
            if batch.finished_at is not None and batch.finished_at < cutoff
        ]
        for batch_id in expired:
            app.state.batches.pop(batch_id, None)
        return sorted(app.state.batches.values(), key=lambda batch: batch.created_at, reverse=True)

    @app.get("/api/batches/{batch_id}", response_model=BatchOut)
    def get_batch(batch_id: str) -> BatchOut:
        batch = cast(BatchOut | None, app.state.batches.get(batch_id))
        if batch is None:
            raise HTTPException(404, "그런 업로드 작업이 없습니다.")
        return batch

    @app.post("/api/scan", response_model=list[IngestOut])
    async def scan(engine: Engine) -> list[IngestOut]:
        """Read whatever is sitting unprocessed in the inbox, including hand-dropped files."""
        async with app.state.ingest_lock:
            results: list[IngestOut] = []
            maintenance_source = f"scan:{uuid.uuid4().hex[:8]}"
            arrivals = 0
            blocked = load_maintenance(engine.vault.root).status == "failed"
            for rel in engine.ingest.pending_inbox():
                result = await _process(engine, rel)
                results.append(result)
                if _is_new_arrival(result):
                    arrivals += 1
                    state = _enqueue_documents(
                        engine, [result.document_id], source=maintenance_source
                    )
                    if not blocked and window_ready(engine.catalog, state.pending_document_ids):
                        state = await _drain_maintenance(
                            engine, source=maintenance_source, max_windows=1
                        )
                        blocked = state.status == "failed"
            state = load_maintenance(engine.vault.root)
            if not blocked and _tail_due(engine, arrivals, state):
                await _drain_maintenance(engine, source=maintenance_source)
            return results

    @app.get("/api/progress")
    async def progress_stream() -> StreamingResponse:
        """Live ingest steps, so a slow document reads as working rather than hung."""
        return StreamingResponse(
            stream(app.state.progress),
            media_type="text/event-stream",
            # x-accel-buffering: a reverse proxy would otherwise hold the stream until it ends.
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    async def _process(
        engine: Bismuth,
        rel: PurePosixPath,
        *,
        on_progress: Callable[[Progress], None] | None = None,
    ) -> IngestOut:
        publish = on_progress or app.state.progress.publish
        _drain(engine)  # anything left from an earlier document is not this one's bill
        try:
            result = await engine.ingest.process(rel, on_progress=publish)
        except BismuthError as exc:
            publish(Progress(stage=Stage.FAILED, filename=rel.name, note=str(exc)))
            return IngestOut(filename=rel.name, ok=False, reason=str(exc), spend=_drain(engine))
        return _result_of(result, spend=_drain(engine))

    def _is_new_arrival(result: IngestOut) -> bool:
        return result.ok and result.placed and not result.duplicate and bool(result.document_id)

    def _enqueue_documents(
        engine: Bismuth, document_ids: list[str], *, source: str
    ) -> MaintenanceState:
        previous = load_maintenance(engine.vault.root)
        backlog = list(previous.pending_document_ids)
        if previous.status == "failed" and not backlog:
            # A failure written by a pre-window build has no focus IDs. Seed it from
            # durable cards so the next retry still covers the existing collection.
            backlog = [document_id for document_id, _ in engine.catalog.iter_cards()]
        seen = set(backlog)
        for document_id in document_ids:
            if document_id in seen:
                continue
            backlog.append(document_id)
            seen.add(document_id)
        new_cycle = (
            not previous.pending_document_ids
            and previous.status != "failed"
            and previous.source != source
        )
        waiting = previous.model_copy(
            update={
                "status": "failed" if previous.status == "failed" else "waiting",
                "source": source,
                "error": previous.error if previous.status == "failed" else "",
                "summary": "" if new_cycle else previous.summary,
                "moved": 0 if new_cycle else previous.moved,
                "applied": False if new_cycle else previous.applied,
                "pending_document_ids": backlog,
                "completed_windows": 0 if new_cycle else previous.completed_windows,
                "current_window_documents": 0,
                "finished_at": None,
            }
        )
        save_maintenance(engine.vault.root, waiting)
        return waiting

    def _tail_due(engine: Bismuth, arrivals: int, state: MaintenanceState) -> bool:
        if not state.pending_document_ids:
            return False
        has_library_folders = any(
            folder.parts and folder.parts[0] != INBOX.parts[0]
            for folder in engine.vault.iter_folders()
        )
        # Four is not a semantic threshold: the shadow-plan validator requires two
        # non-singleton sibling shelves, which is mathematically impossible below it.
        return arrivals >= 4 or (not has_library_folders and len(state.pending_document_ids) >= 4)

    async def _drain_maintenance(
        engine: Bismuth,
        *,
        source: str,
        max_windows: int | None = None,
    ) -> MaintenanceState:
        """Process queued arrivals in isolated windows, updating the tree between them."""
        processed = 0
        while True:
            previous = load_maintenance(engine.vault.root)
            if not previous.pending_document_ids:
                return previous
            window = next_window(engine.catalog, previous.pending_document_ids)
            if not window:
                return previous
            running = previous.model_copy(
                update={
                    "status": "running",
                    "source": source,
                    "error": "",
                    "attempts": previous.attempts + 1,
                    "current_window_documents": len(window),
                    "started_at": time.time(),
                    "finished_at": None,
                }
            )
            save_maintenance(engine.vault.root, running)
            logger.info(
                "maintenance window started: source=%s window=%d documents=%d queued=%d",
                source,
                running.completed_windows + 1,
                len(window),
                len(running.pending_document_ids),
            )
            try:
                result = await engine.agent.reorganize(focus_document_ids=window)
            except asyncio.CancelledError:
                failed = running.model_copy(
                    update={
                        "status": "failed",
                        "error": (
                            "The server stopped while organizing. "
                            "The saved arrival windows can be retried."
                        ),
                        "current_window_documents": 0,
                        "finished_at": time.time(),
                    }
                )
                save_maintenance(engine.vault.root, failed)
                raise
            except Exception as exc:
                logger.exception("autonomous library maintenance failed; preserving current tree")
                failed = running.model_copy(
                    update={
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "current_window_documents": 0,
                        "finished_at": time.time(),
                    }
                )
                save_maintenance(engine.vault.root, failed)
                return failed
            finally:
                _drain(engine)

            problems = result.proposal.problems
            if not result.applied and (problems or not result.proposal.summary.strip()):
                detail = "; ".join(problems) or "planner returned no plan and no explanation"
                failed = running.model_copy(
                    update={
                        "status": "failed",
                        "error": f"Structure plan was not completed: {detail}",
                        "summary": result.proposal.summary,
                        "current_window_documents": 0,
                        "finished_at": time.time(),
                    }
                )
                save_maintenance(engine.vault.root, failed)
                return failed

            consumed = set(window)
            remaining = [
                document_id
                for document_id in running.pending_document_ids
                if document_id not in consumed
            ]
            processed += 1
            complete = not remaining
            state = running.model_copy(
                update={
                    "status": "done" if complete else "waiting",
                    "summary": result.proposal.summary,
                    "moved": running.moved + result.moved,
                    "applied": running.applied or result.applied,
                    "pending_document_ids": remaining,
                    "completed_windows": running.completed_windows + 1,
                    "current_window_documents": 0,
                    "finished_at": time.time() if complete else None,
                }
            )
            save_maintenance(engine.vault.root, state)
            logger.info(
                "maintenance window finished: source=%s window=%d moved=%d remaining=%d",
                source,
                state.completed_windows,
                result.moved,
                len(remaining),
            )
            if complete or (max_windows is not None and processed >= max_windows):
                return state

    def _drain(engine: Bismuth) -> Spend:
        """Collect and reset what the models have spent. Documents are processed one at a
        time, so draining around one is what attributes the bill to it.

        Draining is also what keeps the adapters' usage lists from growing for the life of
        the process; before anything read them, nothing ever emptied them.
        """
        spend = Spend.of(engine.llm.drain_usage())
        chat_drain = getattr(engine.chat, "drain_usage", None)
        if chat_drain is not None:  # agentkit's ChatModel protocol does not require it
            spend = spend + Spend.of(chat_drain())
        engine.ledger.record(spend)
        return spend

    @app.post("/api/delete")
    async def delete(body: DeleteIn, engine: Engine) -> dict[str, Any]:
        """Delete a file or a folder. Reversible via the journal, like everything else."""
        rel = PurePosixPath(body.path)
        try:
            result = await (
                engine.deletion.delete_folder(rel)
                if body.is_folder
                else engine.deletion.delete_file(rel)
            )
        except BismuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"path": result.path, "files": result.files, "folders": result.folders}

    @app.post("/api/delete-many")
    async def delete_many(body: DeleteManyIn, engine: Engine) -> dict[str, Any]:
        """Delete several documents in one reversible batch."""
        try:
            result = await engine.deletion.delete_files([PurePosixPath(p) for p in body.paths])
        except BismuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"files": result.files}

    @app.post("/api/delete-folders")
    async def delete_folders(body: DeleteManyIn, engine: Engine) -> dict[str, Any]:
        """Delete several folders, and everything under them, in one reversible batch."""
        try:
            result = await engine.deletion.delete_folders([PurePosixPath(p) for p in body.paths])
        except BismuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"files": result.files, "folders": result.folders}

    @app.post("/api/move")
    async def move(body: MoveIn, engine: Engine) -> dict[str, Any]:
        """Move documents into a folder the user chose. Reversible via the journal."""
        try:
            result = await engine.move.move([PurePosixPath(p) for p in body.paths], body.target)
        except BismuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"target": result.target, "moved": result.moved}

    @app.post("/api/organize/propose")
    async def organize_propose(body: OrganizeIn, engine: Engine) -> dict[str, Any]:
        """Let the agent inspect the vault and return a reorganisation plan. Moves nothing."""
        instruction = (
            f"Review the structure of '{body.folder}' and propose any reorganisation it needs."
            if body.folder
            else DEFAULT_ORGANIZE_INSTRUCTION
        )
        proposal = await engine.agent.propose_reorg(instruction, scope=body.folder)
        return {
            "summary": proposal.summary,
            "moves": [{"paths": m.paths, "target": m.target} for m in proposal.moves],
            "renames": [{"folder": r.folder, "new_name": r.new_name} for r in proposal.renames],
            "problems": proposal.problems,
        }

    @app.post("/api/organize/apply")
    async def organize_apply(body: ApplyIn, engine: Engine) -> dict[str, Any]:
        """Apply an approved reorganisation plan. Each change is journalled and undoable."""
        applied = 0
        try:
            for item in body.moves:
                result = await engine.move.move([PurePosixPath(p) for p in item.paths], item.target)
                applied += result.moved
            for rename in body.renames:
                await engine.move.rename_folder(PurePosixPath(rename.folder), rename.new_name)
                applied += 1
        except BismuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"applied": applied}

    @app.get("/api/journal", response_model=list[dict[str, Any]])
    def journal(engine: Engine, limit: int = 30) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in engine.journal.iter_entries(limit=limit)]

    @app.post("/api/journal/{entry_id}/undo")
    def undo(entry_id: str, engine: Engine) -> dict[str, Any]:
        """Reverse a change. Any change, including a previous undo."""
        try:
            return engine.transactor.undo(entry_id).model_dump(mode="json")
        except BismuthError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # The UI is a single embedded file with no hashed asset URL. An already-open
        # tab naturally keeps running its old JavaScript, and ordinary reloads may also
        # reuse a cached response after a server upgrade. Always return a fresh shell;
        # vault data has its own API and is unaffected.
        return FileResponse(
            STATIC / "index.html",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )

    return app


class StatusOut(BaseModel):
    configured: bool
    vault: str
    documents: int
    folders: int
    inbox: int
    unprocessed: int
    placed: int
    unplaced: int
    runs_locally: bool
    supported_formats: list[str]
    spend: Spend = Spend()
    """Everything this vault has cost, not just this tab's share of it."""


class FolderOut(BaseModel):
    path: str
    name: str
    depth: int
    files: int
    purpose: str


class DocumentOut(BaseModel):
    filename: str
    path: str
    title: str = ""
    doc_type: str = ""
    summary: str = ""
    topics: list[str] = []

    @classmethod
    def of(cls, engine: Bismuth, rel: PurePosixPath) -> DocumentOut:
        base = cls(filename=rel.name, path=str(rel))
        sidecar = rel.parent / sidecar_name(rel.name)
        if not engine.vault.exists(sidecar):
            return base
        meta = read_sidecar_meta(engine.vault.read_text(sidecar))
        if not meta:
            return base
        card = engine.catalog.load_card(str(meta.get("document_id", "")))
        # Falls back to sidecar frontmatter when the card cache is gone (e.g. after an undone delete).
        raw_topics = meta.get("topics")
        meta_topics = [str(x) for x in raw_topics] if isinstance(raw_topics, list) else []
        return cls(
            filename=rel.name,
            path=str(rel),
            title=card.title if card else str(meta.get("title", "")),
            doc_type=card.doc_type if card else str(meta.get("doc_type", "")),
            summary=card.summary if card else "",
            topics=list(card.topics) if card else meta_topics,
        )


class FolderDetailOut(BaseModel):
    path: str
    charter: dict[str, Any] | None
    documents: list[DocumentOut]


class IngestOut(BaseModel):
    filename: str
    ok: bool
    document_id: str = ""
    destination: str = ""
    placed: bool = False
    created_folder: bool = False
    reason: str = ""
    duplicate: bool = False
    spend: Spend = Spend()


class BatchOut(BaseModel):
    id: str
    total: int
    filenames: list[str] = Field(default_factory=list)
    completed: int = 0
    failed: int = 0
    duplicate: int = 0
    inbox: int = 0
    status: str = "queued"
    current: str = ""
    current_stage: str = ""
    current_label: str = ""
    created_at: float
    finished_at: float | None = None


class MaintenanceOut(BaseModel):
    status: str
    source: str
    error: str
    summary: str
    attempts: int
    moved: int
    applied: bool
    pending_documents: int
    completed_windows: int
    current_window_documents: int
    started_at: float | None
    finished_at: float | None

    @classmethod
    def of(cls, state: MaintenanceState) -> MaintenanceOut:
        return cls(
            status=state.status,
            source=state.source,
            error=state.error,
            summary=state.summary,
            attempts=state.attempts,
            moved=state.moved,
            applied=state.applied,
            pending_documents=len(state.pending_document_ids),
            completed_windows=state.completed_windows,
            current_window_documents=state.current_window_documents,
            started_at=state.started_at,
            finished_at=state.finished_at,
        )


class SetupStateOut(BaseModel):
    configured: bool
    providers: list[dict[str, Any]]
    provider_id: str = ""
    api_key_tail: str = ""
    api_base: str | None = None
    api_headers: dict[str, str] = Field(default_factory=dict)
    api_body: dict[str, Any] = Field(default_factory=dict)
    native_schema: bool | None = None
    model: str = ""
    vault_path: str = ""


class ProviderCheckIn(BaseModel):
    provider_id: str
    api_key: str = ""
    api_base: str | None = None
    api_headers: dict[str, str] = Field(default_factory=dict)
    reuse_saved_key: bool = False


class ProviderCheckOut(BaseModel):
    ok: bool
    error: str = ""
    models: list[str] = []
    suggested_model: str = ""


class DeleteIn(BaseModel):
    path: str
    is_folder: bool = False


class DeleteManyIn(BaseModel):
    paths: list[str]


class MoveIn(BaseModel):
    paths: list[str]
    target: str


class OrganizeIn(BaseModel):
    folder: str = ""


class MoveItem(BaseModel):
    paths: list[str]
    target: str


class RenameItem(BaseModel):
    folder: str
    new_name: str


class ApplyIn(BaseModel):
    moves: list[MoveItem] = []
    renames: list[RenameItem] = []


class SetupIn(BaseModel):
    provider_id: str
    api_key: str = ""
    reuse_saved_key: bool = False
    api_base: str | None = None
    api_headers: dict[str, str] = Field(default_factory=dict)
    api_body: dict[str, Any] = Field(default_factory=dict)
    model: str
    vault_path: str


def _result_of(result: IngestResult, *, spend: Spend) -> IngestOut:
    return IngestOut(
        spend=spend,
        filename=result.filename,
        ok=True,
        document_id=result.document_id,
        destination=str(result.destination),
        placed=result.placement.is_placed,
        created_folder=result.placement.created_folder,
        reason=result.placement.rationale,
        duplicate=result.duplicate,
    )
