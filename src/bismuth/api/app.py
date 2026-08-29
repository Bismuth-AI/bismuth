"""Local HTTP API for a Bismuth vault."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, cast
from urllib.parse import quote, urlsplit

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bismuth import __version__
from bismuth.adapters.llm import (
    list_models,
    litellm_adapter,
    probe_model,
    suggest_model,
)
from bismuth.api import diagnostics
from bismuth.api.progress import ProgressBus, stream
from bismuth.api.progress import label as progress_label
from bismuth.config import (
    PROVIDERS,
    ApiMode,
    ReasoningEffort,
    Settings,
    UserConfig,
    load_env_file,
    provider,
    save_user_config,
)
from bismuth.container import Bismuth, build
from bismuth.domain.document import DocumentCard, sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Actor, JournalEntry
from bismuth.domain.progress import Progress, Stage
from bismuth.logging_setup import configure_logging, finish_run_manifest, update_run_manifest
from bismuth.ports.llm import CURRENT_USAGE, Spend, Usage
from bismuth.ports.vault import INBOX
from bismuth.prompts.agent import DEFAULT_ORGANIZE_INSTRUCTION
from bismuth.services import replay as replay_service
from bismuth.services import simple as simple_service
from bismuth.services.ingest import Prepared
from bismuth.services.sidecar import read_sidecar_meta

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
FOLDER_PAGE_SIZE = 100
FOLDER_PAGE_MAX = 200


def _open_in_file_manager(path: Path) -> None:
    """Show an existing directory in the host operating system's file manager."""
    directory = path.resolve(strict=True)
    if not directory.is_dir():
        raise NotADirectoryError(directory)

    if sys.platform == "win32":
        os.startfile(directory)
        return

    command = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen(
        [command, str(directory)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _host_of(url: str | None) -> str:
    """Scheme, host and port. Userinfo and query are credentials often enough."""
    parsed = urlsplit(url or "")
    if not parsed.hostname:
        return ""
    host = f"{parsed.scheme}://{parsed.hostname}"
    return f"{host}:{parsed.port}" if parsed.port else host


def _diagnostic_settings(settings: Settings) -> dict[str, Any]:
    """Manifest-safe runtime settings; credentials and URL userinfo/query never enter logs."""
    endpoint = _host_of(settings.api_base)
    answering = settings.chat()
    generation = {
        key: value
        for key, value in settings.api_body.items()
        if key
        in {
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "presence_penalty",
            "frequency_penalty",
            "max_tokens",
            "max_completion_tokens",
            "chat_template_kwargs",
        }
    }
    return {
        "vault_path": str(settings.vault_path),
        "provider_id": settings.provider_id,
        "api_endpoint": endpoint,
        "model": settings.model,
        "api_mode": settings.api_mode,
        "reasoning_effort": settings.reasoning_effort,
        # Record both workloads when they use different endpoints.
        "chat_model": answering.model,
        "chat_endpoint": _host_of(answering.api_base),
        "chat_api_mode": settings.chat_api_mode,
        "chat_reasoning_effort": settings.chat_reasoning_effort,
        "chat_is_separate": settings.chat_is_separate,
        "native_schema": settings.native_schema,
        "llm_timeout_seconds": settings.llm_timeout_seconds,
        "llm_absolute_timeout_seconds": settings.llm_absolute_timeout_seconds,
        "llm_max_schema_retries": settings.llm_max_schema_retries,
        "llm_max_concurrency": settings.llm_max_concurrency,
        "card_context_chars": settings.card_context_chars,
        "card_max_windows": settings.card_max_windows,
        "generation": generation,
    }


def _preload(engine: Bismuth) -> None:
    """Load deferred adapters before accepting requests."""
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


ACCEPTED_UPLOADS = frozenset({".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".txt", ".md"})
"""Formats supported by the complete web upload flow."""

MAX_UPLOAD_FILES = 500
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 500 * 1024 * 1024


def _accept(files: list[UploadFile], accepted: frozenset[str]) -> None:
    """Validate upload metadata before staging any file."""
    if not files:
        raise HTTPException(400, "올릴 파일이 없습니다.")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(413, f"한 번에 {MAX_UPLOAD_FILES}개까지 올릴 수 있습니다.")
    refused = sorted(
        {
            Path(f.filename or "").suffix.lower() or "(확장자 없음)"
            for f in files
            if Path(f.filename or "").suffix.lower() not in accepted
        }
    )
    if refused:
        allowed = ", ".join(sorted(accepted))
        raise HTTPException(
            400, f"지원하지 않는 형식입니다: {', '.join(refused)} (허용: {allowed})"
        )
    known_sizes = [file.size for file in files if file.size is not None]
    if any(size > MAX_UPLOAD_BYTES for size in known_sizes):
        raise HTTPException(413, "파일 하나의 최대 크기는 50MB입니다.")
    if len(known_sizes) == len(files) and sum(known_sizes) > MAX_UPLOAD_TOTAL_BYTES:
        raise HTTPException(413, "한 번에 올릴 수 있는 전체 크기는 500MB입니다.")


async def _read_upload(upload: UploadFile) -> bytes:
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "파일 하나의 최대 크기는 50MB입니다.")
    return data


async def _validate_upload_contents(files: list[UploadFile]) -> None:
    """Validate file signatures before staging any part of a request."""
    ole_signature = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    for upload in files:
        header = await upload.read(1024)
        await upload.seek(0)
        suffix = Path(upload.filename or "").suffix.lower()
        name = Path(upload.filename or "").name
        if suffix == ".pdf" and b"%PDF-" not in header:
            raise HTTPException(400, f"올바른 PDF 파일이 아닙니다: {name}")
        if suffix in {".hwp", ".doc"} and not header.startswith(ole_signature):
            raise HTTPException(400, f"올바른 {suffix.upper()[1:]} 파일이 아닙니다: {name}")
        if suffix in {".hwpx", ".docx"} and not header.startswith(b"PK"):
            raise HTTPException(400, f"올바른 {suffix.upper()[1:]} 파일이 아닙니다: {name}")


def _is_local_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
    except ValueError:
        return False


def create_app(
    settings: Settings,
    *,
    verbose: bool = False,
    accepted_uploads: frozenset[str] = ACCEPTED_UPLOADS,
) -> FastAPI:
    load_env_file()
    engine = build(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Configure after Uvicorn so its logging setup cannot replace these handlers.
        configure_logging(verbose=verbose, continue_active_run=True)
        update_run_manifest(**_diagnostic_settings(settings))
        _preload(engine)
        if recovered := engine.recover():
            logger.warning("rolled back %d interrupted change(s) from a previous run", recovered)
        try:
            yield
        finally:
            tasks: set[asyncio.Task[None]] = getattr(app.state, "batch_tasks", set())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await litellm_adapter.close_clients()
            finish_run_manifest()

    app = FastAPI(title="Bismuth", version=__version__, lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"],
    )
    app.state.engine = engine
    app.state.settings = settings
    app.state.progress = ProgressBus()
    app.state.ingest_lock = asyncio.Lock()
    app.state.batches = {}
    app.state.batch_tasks = set()
    app.state.search_cache = ((), [])
    app.include_router(diagnostics.router)

    @app.middleware("http")
    async def local_origin_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        origin = request.headers.get("origin")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and origin
            and not _is_local_origin(origin)
        ):
            return JSONResponse(
                status_code=403, content={"detail": "External origins are not allowed."}
            )
        response = await call_next(request)
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("x-frame-options", "DENY")
        response.headers.setdefault("referrer-policy", "no-referrer")
        response.headers.setdefault(
            "content-security-policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        return response

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

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
            api_mode=current.api_mode,
            reasoning_effort=current.reasoning_effort,
            native_schema=current.native_schema,
            api_key_tail=f"…{current.api_key[-4:]}" if current.api_key else "",
            chat_provider_id=current.chat_provider_id,
            chat_model=current.chat_model,
            chat_api_base=current.chat_api_base,
            chat_api_headers=current.chat_api_headers,
            chat_api_body=current.chat_api_body,
            chat_api_mode=current.chat_api_mode,
            chat_reasoning_effort=current.chat_reasoning_effort,
            chat_api_key_tail=f"…{current.chat_api_key[-4:]}" if current.chat_api_key else "",
            chat_is_separate=current.chat_is_separate,
        )

    @app.post("/api/setup/check", response_model=ProviderCheckOut)
    async def setup_check(body: ProviderCheckIn) -> ProviderCheckOut:
        """Ask the provider what this key can reach. Listing the catalogue is the check."""
        chosen = provider(body.provider_id)
        if chosen is None:
            raise HTTPException(400, f"알 수 없는 프로바이더: {body.provider_id}")
        saved = app.state.settings
        remembered = saved.chat_api_key if body.for_chat else saved.api_key
        key = body.api_key or (remembered if body.reuse_saved_key else "")
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

        # A credential that lists the catalogue still says nothing about the model
        # chosen from it, and everything downstream assumes the model answers. Ask
        # it once here, where the answer is a message in the wizard rather than a
        # failed document hours later. The same call tells us whether a self-hosted
        # endpoint constrains decoding to a schema, which LiteLLM's table cannot.
        probe = await anyio.to_thread.run_sync(
            lambda: probe_model(
                chosen.id,
                model=body.model,
                api_key=key,
                api_base=body.api_base or chosen.default_api_base,
                headers=body.api_headers,
            )
        )
        if not probe.ok and not body.force:
            raise HTTPException(400, f"{body.model} 호출에 실패했습니다 — {probe.error}")
        native = probe.native_schema
        logger.info("%s answers: %s (schema: %s)", body.model, probe.ok, native)

        answers = UserConfig(
            vault_path=Path(body.vault_path).expanduser(),
            provider_id=chosen.id,
            api_key=key,
            api_base=body.api_base or chosen.default_api_base,
            api_headers=body.api_headers,
            api_body=body.api_body,
            api_mode=body.api_mode,
            reasoning_effort=body.reasoning_effort,
            native_schema=native,
            model=body.model,
            chat_model=body.chat_model.strip(),
        )
        if not answers.is_configured:
            raise HTTPException(400, "모델을 골라 주세요.")

        # Re-read the saved file so replaced dictionaries stay replaced.
        save_user_config(answers)
        updated = Settings()
        app.state.settings = updated
        update_run_manifest(**_diagnostic_settings(updated))
        app.state.engine = build(updated)
        # Warm the new engine before returning control to the UI.
        _preload(app.state.engine)
        logger.info("configuration updated: %s", updated.redacted())
        return setup_state()

    @app.post("/api/setup/chat", response_model=SetupStateOut)
    async def setup_chat(body: ChatSetupIn) -> SetupStateOut:
        """Configure the answering model without changing filing settings."""
        current = app.state.settings
        if not current.is_configured:
            raise HTTPException(400, "먼저 볼트 설정을 마쳐 주세요.")

        chosen = provider(body.provider_id) if body.provider_id else None
        if body.provider_id and chosen is None:
            raise HTTPException(400, f"알 수 없는 프로바이더: {body.provider_id}")

        model = body.model.strip()
        # Where the probe below should be sent. Filing and answering may be
        # different models on different addresses, so the filing probe said
        # nothing about this one.
        target: dict[str, Any] = {
            "provider_id": current.provider_id,
            "model": model or current.model,
            "api_key": current.api_key,
            "api_base": current.api_base,
            "headers": current.api_headers,
        }
        if chosen is None:
            # Same provider as filing. The same model name is not a second model;
            # storing it would only mean the two drift apart when one is changed.
            answering = UserConfig(
                vault_path=current.vault_path,
                provider_id=current.provider_id,
                api_key=current.api_key,
                api_base=current.api_base,
                api_headers=current.api_headers,
                api_body=current.api_body,
                api_mode=current.api_mode,
                reasoning_effort=current.reasoning_effort,
                native_schema=current.native_schema,
                model=current.model,
                chat_model="" if model == current.model else model,
                chat_api_mode=body.api_mode,
                chat_reasoning_effort=body.reasoning_effort,
            )
        else:
            key = body.api_key or (current.chat_api_key if body.reuse_saved_key else "")
            if chosen.needs_key and not key:
                raise HTTPException(400, f"{chosen.label} 에는 키가 필요합니다.")
            base = body.api_base or chosen.default_api_base
            if chosen.needs_api_base and not base:
                raise HTTPException(400, f"{chosen.label} 에는 엔드포인트 주소가 필요합니다.")
            if not model:
                raise HTTPException(400, "모델을 골라 주세요.")
            target = {
                "provider_id": chosen.id,
                "model": model,
                "api_key": key,
                "api_base": base,
                "headers": body.api_headers,
            }
            answering = UserConfig(
                vault_path=current.vault_path,
                provider_id=current.provider_id,
                api_key=current.api_key,
                api_base=current.api_base,
                api_headers=current.api_headers,
                api_body=current.api_body,
                api_mode=current.api_mode,
                reasoning_effort=current.reasoning_effort,
                native_schema=current.native_schema,
                model=current.model,
                chat_provider_id=chosen.id,
                chat_model=model,
                chat_api_key=key,
                chat_api_base=base,
                chat_api_headers=body.api_headers,
                chat_api_body=body.api_body,
                chat_api_mode=body.api_mode,
                chat_reasoning_effort=body.reasoning_effort,
            )

        probe = await anyio.to_thread.run_sync(lambda: probe_model(**target))
        if not probe.ok and not body.force:
            raise HTTPException(400, f"{target['model']} 호출에 실패했습니다 — {probe.error}")

        save_user_config(answering)
        updated = Settings()
        app.state.settings = updated
        update_run_manifest(**_diagnostic_settings(updated))
        app.state.engine = build(updated)
        _preload(app.state.engine)
        logger.info("answering model is now %s", updated.chat().model)
        return setup_state()

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
            supported_formats=sorted(accepted_uploads & engine.parsers.supported_extensions()),
            spend=engine.ledger.total(),
        )

    @app.post("/api/vault/open")
    async def open_vault(engine: Engine) -> dict[str, str]:
        """Open the active vault root in the host operating system's file manager."""
        root = Path(engine.vault.root)
        try:
            await anyio.to_thread.run_sync(_open_in_file_manager, root)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("could not open vault in file manager: %s", exc)
            raise HTTPException(500, "볼트 폴더를 파일 탐색기에서 열지 못했습니다.") from exc
        return {"opened": str(root)}

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
    def folder(
        path: str,
        engine: Engine,
        offset: int = 0,
        limit: int = FOLDER_PAGE_SIZE,
    ) -> FolderDetailOut:
        rel = PurePosixPath(path) if path not in ("", "/") else PurePosixPath()
        if not engine.vault.is_dir(rel):
            raise HTTPException(404, f"그런 폴더가 없습니다: {path}")
        offset = max(0, offset)
        limit = max(1, min(limit, FOLDER_PAGE_MAX))
        files = list(engine.vault.iter_files(rel))
        page = files[offset : offset + limit]
        charter = None
        try:
            charter = engine.charters.load(rel)
        except BismuthError as exc:
            logger.warning("unreadable folder note at %s: %s", rel, exc)
        return FolderDetailOut(
            path=str(rel),
            charter=charter.model_dump(mode="json") if charter else None,
            documents=[DocumentOut.of(engine, file) for file in page],
            total=len(files),
            offset=offset,
            limit=limit,
            has_more=offset + len(page) < len(files),
        )

    @app.get("/api/search", response_model=list[DocumentOut])
    def search(engine: Engine, q: str = "", limit: int = 100) -> list[DocumentOut]:
        """Find documents from saved card metadata without reopening their originals."""
        needle = q.strip().casefold()
        if not needle:
            return []
        limit = max(1, min(limit, 100))
        files = list(engine.vault.iter_files(PurePosixPath(), recursive=True))
        signature = tuple(str(rel) for rel in files)
        cached_signature, cached_documents = app.state.search_cache
        if cached_signature != signature:
            cached_documents = [DocumentOut.of(engine, rel, prefer_catalog=False) for rel in files]
            app.state.search_cache = (signature, cached_documents)
        ranked: list[tuple[int, str, DocumentOut]] = []
        for document in cast(list[DocumentOut], cached_documents):
            filename = document.filename.casefold()
            title = document.title.casefold()
            path = document.path.casefold()
            doc_type = document.doc_type.casefold()
            topics = [topic.casefold() for topic in document.topics]
            summary = document.summary.casefold()
            if needle in (filename, title):
                score = 0
            elif filename.startswith(needle) or title.startswith(needle):
                score = 1
            elif needle in topics:
                score = 2
            elif needle in filename or needle in title:
                score = 3
            elif needle in path:
                score = 4
            elif needle in doc_type or any(needle in topic for topic in topics):
                score = 5
            elif needle in summary:
                score = 6
            else:
                continue
            ranked.append((score, path, document))
        ranked.sort(key=lambda found: (found[0], found[1]))
        return [document for _score, _path, document in ranked[:limit]]

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
        is_pdf = rel.suffix.lower() == ".pdf"
        media = "application/pdf" if is_pdf else "application/octet-stream"
        disposition = f"{'inline' if is_pdf else 'attachment'}; filename*=UTF-8''{quote(rel.name)}"
        return Response(
            data,
            media_type=media,
            headers={
                "content-disposition": disposition,
                "x-content-type-options": "nosniff",
            },
        )

    async def run_batch(batch_id: str, staged: list[PurePosixPath], engine: Bismuth) -> None:
        """Process already-safe inbox files independently of the browser connection."""
        batch: BatchOut = app.state.batches[batch_id]
        batch.status = "queued"
        update_run_manifest(
            status="running",
            activity_status="processing",
            active_batch={"id": batch_id, "documents": len(staged), "status": "queued"},
        )

        def report(progress: Progress) -> None:
            batch.current = progress.filename
            batch.current_stage = progress.stage.value
            batch.current_label = progress_label(progress)
            app.state.progress.publish(progress)

        try:
            async with app.state.ingest_lock:
                batch.status = "running"
                # Prepare concurrently, then file in input order because each result changes the tree.
                prepared: asyncio.Queue[tuple[PurePosixPath, Prepared | Exception]] = asyncio.Queue(
                    maxsize=engine.settings.ingest_read_ahead
                )

                async def read_ahead() -> None:
                    done: dict[int, tuple[PurePosixPath, Prepared | Exception]] = {}
                    next_out = 0
                    gate = asyncio.Semaphore(engine.settings.ingest_read_ahead)
                    lock = asyncio.Lock()

                    async def one(index: int, rel: PurePosixPath) -> None:
                        nonlocal next_out
                        async with gate:
                            try:
                                outcome: Prepared | Exception = await engine.ingest.prepare(
                                    rel, on_progress=report
                                )
                            except Exception as exc:  # reported per document, as filing is
                                outcome = exc
                        async with lock:
                            done[index] = (rel, outcome)
                            while next_out in done:
                                await prepared.put(done.pop(next_out))
                                next_out += 1

                    await asyncio.gather(*(one(i, rel) for i, rel in enumerate(staged)))

                reader = asyncio.create_task(read_ahead())
                # Filed in tens, because a class is only visible in several: asked about
                # one document the only honest answer is its title, and a tree of titles is
                # the list the folders were supposed to replace.
                pending: list[tuple[PurePosixPath, DocumentCard, Prepared]] = []
                seen_hashes: set[str] = set()

                async def flush() -> None:
                    if not pending:
                        return
                    taken = list(pending)
                    pending.clear()
                    try:
                        await engine.simple.file(taken)
                    except Exception as exc:
                        logger.exception("batch %s failed while filing %d", batch_id, len(taken))
                        for rel, _, _ in taken:
                            report(Progress(stage=Stage.FAILED, filename=rel.name, note=str(exc)))
                        batch.completed += len(taken)
                        batch.failed += len(taken)
                        return
                    batch.completed += len(taken)
                    _drain(engine)
                    await engine.simple.regroup()
                    _drain(engine)
                    if engine.simple.due():
                        report(Progress(stage=Stage.DIVIDING, filename="", note="전체 구조 점검"))
                        await engine.simple.review()
                        _drain(engine)

                for _ in staged:
                    rel, outcome = await prepared.get()
                    batch.current = rel.name
                    if isinstance(outcome, Exception):
                        logger.error(
                            "batch %s failed while reading %s",
                            batch_id,
                            rel,
                            exc_info=(type(outcome), outcome, outcome.__traceback__),
                        )
                        report(Progress(stage=Stage.FAILED, filename=rel.name, note=str(outcome)))
                        batch.completed += 1
                        batch.failed += 1
                        continue
                    duplicate = bool(outcome.duplicate_of) or outcome.source.sha256 in seen_hashes
                    if duplicate or outcome.card is None:
                        if duplicate:
                            engine.ingest.discard_duplicate(outcome.rel)
                        batch.completed += 1
                        batch.duplicate += 1
                        continue
                    seen_hashes.add(outcome.source.sha256)
                    pending.append((outcome.rel, outcome.card, outcome))
                    if len(pending) >= simple_service.BATCH:
                        await flush()
                await flush()
                await reader
                if engine.simple.due(settling=True):
                    report(Progress(stage=Stage.DIVIDING, filename="", note="마지막 구조 점검"))
                    await engine.simple.review(ending=True)
                    _drain(engine)
                else:
                    await engine.simple.regroup(ending=True)
                    _drain(engine)
            batch.status = "done"
        except asyncio.CancelledError:
            batch.status = "interrupted"
            raise
        except Exception:
            batch.status = "failed"
            logger.exception("batch %s stopped unexpectedly", batch_id)
        finally:
            batch.finished_at = time.time()
            update_run_manifest(
                status="idle" if batch.status == "done" else batch.status,
                activity_status="idle",
                active_batch=None,
                last_batch={
                    "id": batch_id,
                    "documents": len(staged),
                    "status": batch.status,
                    "completed": batch.completed,
                    "failed": batch.failed,
                    "finished_at": batch.finished_at,
                },
            )
            if batch.status == "done":
                batch.current = ""
                batch.current_stage = "done"
                batch.current_label = "모든 파일 정리 완료"

    async def run_refile(batch_id: str, engine: Bismuth) -> None:
        """Rebuild the tree from saved cards using the normal filing workflow."""
        batch: BatchOut = app.state.batches[batch_id]
        batch.status = "queued"
        update_run_manifest(
            status="running",
            activity_status="refiling",
            active_batch={"id": batch_id, "documents": batch.total, "status": "queued"},
        )
        try:
            async with app.state.ingest_lock:
                batch.status = "running"
                emptied = replay_service.emptying(engine.vault, into=INBOX)
                if emptied.operations:
                    engine.transactor.execute(
                        JournalEntry(
                            actor=Actor.USER,
                            reason="rebuild folders from saved cards",
                            operations=emptied.operations,
                        )
                    )
                prepared = replay_service.read_prepared(engine.vault, engine.catalog, under=INBOX)
                batch.total = len(prepared)
                engine.simple.forget_reviews()

                for start in range(0, len(prepared), simple_service.BATCH):
                    chunk = prepared[start : start + simple_service.BATCH]
                    filing = [
                        (item.rel, item.card, item) for item in chunk if item.card is not None
                    ]
                    await engine.simple.file(filing)
                    batch.completed += len(filing)
                    batch.current = filing[-1][0].name if filing else ""
                    _drain(engine)
                    await engine.simple.regroup()
                    _drain(engine)
                    app.state.progress.publish(
                        Progress(
                            stage=Stage.PLACED,
                            filename=batch.current,
                            note=f"{batch.completed}/{batch.total} 재배치",
                        )
                    )
                    if engine.simple.due():
                        await engine.simple.review()
                        _drain(engine)

                if engine.simple.due(settling=True):
                    await engine.simple.review(ending=True)
                    _drain(engine)
                else:
                    await engine.simple.regroup(ending=True)
                    _drain(engine)
                batch.status = "done"
        except asyncio.CancelledError:
            batch.status = "interrupted"
            raise
        except Exception:
            batch.status = "failed"
            logger.exception("refile %s stopped unexpectedly", batch_id)
        finally:
            batch.finished_at = time.time()
            batch.current = "" if batch.status == "done" else batch.current
            batch.current_stage = "done" if batch.status == "done" else batch.current_stage
            batch.current_label = (
                "카드 기반 재배치 완료" if batch.status == "done" else batch.current_label
            )
            update_run_manifest(
                status="idle" if batch.status == "done" else batch.status,
                activity_status="idle",
                active_batch=None,
            )

    @app.post("/api/batches", response_model=BatchOut, status_code=202)
    async def create_batch(files: list[UploadFile], engine: Engine) -> BatchOut:
        """Stage a whole selection, then let the server finish it after a page refresh."""
        _accept(files, accepted_uploads & engine.parsers.supported_extensions())
        await _validate_upload_contents(files)
        staged: list[PurePosixPath] = []
        async with app.state.ingest_lock:
            for upload_file in files:
                data = await _read_upload(upload_file)
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
        """Return active and recent batches."""
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

    @app.post("/api/tree/empty")
    async def empty_tree(engine: Engine) -> dict[str, int]:
        """Move filed documents to the root while preserving their sidecars."""
        async with app.state.ingest_lock:
            emptied = replay_service.emptying(engine.vault)
            if emptied.operations:
                engine.transactor.execute(
                    JournalEntry(
                        actor=Actor.USER,
                        reason="empty folder tree for testing",
                        operations=emptied.operations,
                    )
                )
            engine.simple.forget_reviews()
        return {"documents": emptied.documents, "folders": emptied.folders}

    @app.post("/api/refile-all", response_model=BatchOut, status_code=202)
    async def refile_all(engine: Engine) -> BatchOut:
        """Queue a clean rebuild using cards stored in document sidecars."""
        prepared = replay_service.read_prepared(engine.vault, engine.catalog)
        if not prepared:
            raise HTTPException(400, "저장된 카드가 있는 문서가 없습니다.")
        batch_id = uuid.uuid4().hex[:12]
        batch = BatchOut(
            id=batch_id,
            total=len(prepared),
            filenames=[item.rel.name for item in prepared],
            created_at=time.time(),
        )
        app.state.batches[batch_id] = batch
        task = asyncio.create_task(run_refile(batch_id, engine), name=f"bismuth-refile-{batch_id}")
        app.state.batch_tasks.add(task)
        task.add_done_callback(app.state.batch_tasks.discard)
        return batch.model_copy(deep=True)

    @app.get("/api/progress")
    async def progress_stream() -> StreamingResponse:
        """Live ingest steps, so a slow document reads as working rather than hung."""
        return StreamingResponse(
            stream(app.state.progress),
            media_type="text/event-stream",
            # x-accel-buffering: a reverse proxy would otherwise hold the stream until it ends.
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    def _drain(engine: Bismuth) -> Spend:
        """Collect model usage and attribute it to the current operation."""
        spend = Spend.of(engine.llm.drain_usage())
        chat_drain = getattr(engine.chat, "drain_usage", None)
        if chat_drain is not None:  # Optional adapter cleanup hook.
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
        proposal = await engine.agent.propose_reorg(instruction)
        return {
            "summary": proposal.summary,
            "moves": [{"paths": m.paths, "target": m.target} for m in proposal.moves],
            "renames": [{"folder": r.folder, "new_name": r.new_name} for r in proposal.renames],
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

    @app.post("/api/chat")
    async def chat(body: ChatIn, engine: Engine) -> StreamingResponse:
        """Answer one question and stream its search activity."""
        question = body.message.strip()
        if not question:
            raise HTTPException(400, "질문이 비어 있습니다.")
        steps: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def watch(event: Any) -> None:
            if event.kind in ("tool_call", "tool_result", "tool_error", "compact", "stop"):
                loop.call_soon_threadsafe(
                    steps.put_nowait,
                    {
                        "type": event.kind,
                        "tool": event.data.get("name", ""),
                        "arguments": event.data.get("arguments", {}),
                        "reason": event.data.get("reason", ""),
                        "freed": event.data.get("freed", 0),
                        "preview": event.data.get("preview", ""),
                    },
                )

        def wrote(piece: str) -> None:
            loop.call_soon_threadsafe(steps.put_nowait, {"type": "delta", "text": piece})

        async def answering() -> AsyncIterator[str]:
            turn_usage: list[Usage] = []
            capture = CURRENT_USAGE.set(turn_usage)
            try:
                # Isolate usage accounting for this answer task.
                task = asyncio.create_task(
                    engine.conversation.ask(
                        question,
                        conversation_id=body.conversation_id,
                        on_event=watch,
                        on_text=wrote,
                    )
                )
            finally:
                CURRENT_USAGE.reset(capture)
            task.add_done_callback(lambda _: steps.put_nowait(None))
            while (step := await steps.get()) is not None:
                yield f"data: {json.dumps(step, ensure_ascii=False)}\n\n"
            try:
                conversation, answer = await task
            except Exception as exc:
                _drain(engine)
                logger.exception("chat failed")
                detail = str(exc) if isinstance(exc, BismuthError) else "Chat request failed."
                payload = {"type": "error", "detail": detail}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            spend = Spend.of(turn_usage)
            _drain(engine)
            done = {
                "type": "answer",
                "text": answer,
                "conversation_id": conversation.id,
                "spend": spend.model_dump(mode="json"),
            }
            yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            answering(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.delete("/api/chat/{conversation_id}")
    def forget_chat(conversation_id: str, engine: Engine) -> dict[str, str]:
        """Start over. The tree may have changed under an old transcript anyway."""
        engine.conversation.forget(conversation_id)
        return {"forgotten": conversation_id}

    @app.get("/chat", include_in_schema=False)
    def chat_page() -> FileResponse:
        return FileResponse(STATIC / "chat.html", headers={"Cache-Control": "no-store"})

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

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        """Asked for by name by browsers that ignore the link tag."""
        return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")

    @app.get("/favicon.png", include_in_schema=False)
    def favicon_png() -> FileResponse:
        return FileResponse(STATIC / "favicon.png", media_type="image/png")

    @app.get("/trace", include_in_schema=False)
    def trace() -> FileResponse:
        """The run inspector: every call, its prompt and its reply."""
        return FileResponse(STATIC / "trace.html", headers={"Cache-Control": "no-store"})

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


class ChatIn(BaseModel):
    message: str
    conversation_id: str | None = Field(
        default=None, description="Omit to start a new conversation; echo it back to continue one."
    )


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
    def of(cls, engine: Bismuth, rel: PurePosixPath, *, prefer_catalog: bool = True) -> DocumentOut:
        base = cls(filename=rel.name, path=str(rel))
        sidecar = rel.parent / sidecar_name(rel.name)
        if not engine.vault.exists(sidecar):
            return base
        meta = read_sidecar_meta(engine.vault.read_text(sidecar))
        if not meta:
            return base
        card = (
            engine.catalog.load_card(str(meta.get("document_id", ""))) if prefer_catalog else None
        )
        # Falls back to sidecar frontmatter when the card cache is gone (e.g. after an undone delete).
        raw_topics = meta.get("topics")
        meta_topics = [str(x) for x in raw_topics] if isinstance(raw_topics, list) else []
        return cls(
            filename=rel.name,
            path=str(rel),
            title=card.title if card else str(meta.get("title", "")),
            doc_type=card.doc_type if card else str(meta.get("doc_type", "")),
            summary=card.summary if card else str(meta.get("summary", "")),
            topics=list(card.topics) if card else meta_topics,
        )


class FolderDetailOut(BaseModel):
    path: str
    charter: dict[str, Any] | None
    documents: list[DocumentOut]
    total: int
    offset: int
    limit: int
    has_more: bool


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


class SetupStateOut(BaseModel):
    configured: bool
    providers: list[dict[str, Any]]
    provider_id: str = ""
    api_key_tail: str = ""
    api_base: str | None = None
    api_headers: dict[str, str] = Field(default_factory=dict)
    api_body: dict[str, Any] = Field(default_factory=dict)
    api_mode: ApiMode = "auto"
    reasoning_effort: ReasoningEffort = "auto"
    native_schema: bool | None = None
    model: str = ""
    vault_path: str = ""
    chat_provider_id: str = ""
    chat_model: str = ""
    chat_api_base: str | None = None
    chat_api_headers: dict[str, str] = Field(default_factory=dict)
    chat_api_body: dict[str, Any] = Field(default_factory=dict)
    chat_api_mode: ApiMode = "auto"
    chat_reasoning_effort: ReasoningEffort = "auto"
    chat_api_key_tail: str = ""
    chat_is_separate: bool = False


class ProviderCheckIn(BaseModel):
    for_chat: bool = False
    """Whose saved key ``reuse_saved_key`` means: the answering side's, or filing's."""

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


class ChatSetupIn(BaseModel):
    """The answering side, whole. Empty ``provider_id`` means "the same as filing"."""

    provider_id: str = ""
    model: str = ""
    api_key: str = ""
    reuse_saved_key: bool = False
    api_base: str | None = None
    api_headers: dict[str, str] = Field(default_factory=dict)
    api_body: dict[str, Any] = Field(default_factory=dict)
    api_mode: ApiMode = "auto"
    reasoning_effort: ReasoningEffort = "auto"
    force: bool = False
    """Save an endpoint that did not answer the probe. A local server that is merely down."""


class SetupIn(BaseModel):
    provider_id: str
    api_key: str = ""
    reuse_saved_key: bool = False
    api_base: str | None = None
    api_headers: dict[str, str] = Field(default_factory=dict)
    api_body: dict[str, Any] = Field(default_factory=dict)
    api_mode: ApiMode = "auto"
    reasoning_effort: ReasoningEffort = "auto"
    model: str
    chat_model: str = ""
    """Empty means the same model files documents and answers questions."""
    vault_path: str
    force: bool = False
    """Save an endpoint that did not answer the probe. A local server that is merely down."""
