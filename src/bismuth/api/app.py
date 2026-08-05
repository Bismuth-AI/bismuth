"""The HTTP API and the window onto a vault. Localhost, unauthenticated -- a local tool for local documents."""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from bismuth import __version__
from bismuth.adapters.llm import list_models, suggest_models
from bismuth.api.progress import ProgressBus, stream
from bismuth.config import PROVIDERS, Settings, load_env_file, provider, save_user_config
from bismuth.container import Bismuth, build
from bismuth.domain.document import sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.progress import Progress, Stage
from bismuth.logging_setup import configure_logging
from bismuth.ports.vault import INBOX
from bismuth.services.agent import DEFAULT_ORGANIZE_INSTRUCTION
from bismuth.services.ingest import IngestResult
from bismuth.services.sidecar import read_sidecar_meta

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"


def get_engine(request: Request) -> Bismuth:
    """The engine for this app, from app state. Must stay module-level or FastAPI treats it as a query param (422)."""
    engine: Bismuth = request.app.state.engine
    return engine


Engine = Annotated[Bismuth, Depends(get_engine)]


def create_app(settings: Settings) -> FastAPI:
    load_env_file()
    configure_logging()
    engine = build(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if recovered := engine.recover():
            logger.warning("rolled back %d interrupted change(s) from a previous run", recovered)
        yield

    app = FastAPI(title="Bismuth", version=__version__, lifespan=lifespan)
    app.state.engine = engine
    app.state.settings = settings
    app.state.progress = ProgressBus()

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
            model_fast=current.model_fast,
            model_reasoning=current.model_reasoning,
            vault_path=str(current.vault_path),
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
                chosen.id, api_key=key, api_base=body.api_base or chosen.default_api_base
            )
        )
        fast, reasoning = suggest_models(check.models)
        return ProviderCheckOut(
            ok=check.ok,
            error=check.error,
            models=list(check.models),
            suggested_fast=fast,
            suggested_reasoning=reasoning,
        )

    @app.post("/api/setup", response_model=SetupStateOut)
    def setup_save(body: SetupIn) -> SetupStateOut:
        """Persist the wizard's answers and rebuild the engine around them, in place."""
        chosen = provider(body.provider_id)
        if chosen is None:
            raise HTTPException(400, f"알 수 없는 프로바이더: {body.provider_id}")
        key = body.api_key or (app.state.settings.api_key if body.reuse_saved_key else "")
        if chosen.needs_key and not key:
            raise HTTPException(400, f"{chosen.label} 에는 키가 필요합니다.")

        updated = Settings(
            vault_path=Path(body.vault_path).expanduser(),
            provider_id=chosen.id,
            api_key=key,
            api_base=body.api_base or chosen.default_api_base,
            model_fast=body.model_fast,
            model_reasoning=body.model_reasoning,
        )
        if not updated.is_configured:
            raise HTTPException(400, "두 모델을 각각 골라 주세요.")

        save_user_config(updated)
        app.state.settings = updated
        app.state.engine = build(updated)
        logger.info("configuration updated: %s", updated.redacted())
        return setup_state()

    @app.get("/api/status", response_model=StatusOut)
    def status(engine: Engine) -> StatusOut:
        # Disk, not the card cache -- the two can diverge (e.g. after an undone delete).
        inbox_count = engine.vault.count_files(INBOX, recursive=True)
        placed = sum(
            engine.vault.count_files(f, recursive=False)
            for f in engine.vault.iter_folders()
            if f.parts and f.parts[0] != INBOX.parts[0]
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
        for upload_file in files:
            data = await upload_file.read()
            name = Path(upload_file.filename or "untitled").name
            rel = engine.ingest.stage(data, name)
            results.append(await _process(engine, rel))
        return results

    @app.post("/api/scan", response_model=list[IngestOut])
    async def scan(engine: Engine) -> list[IngestOut]:
        """Read whatever is sitting unprocessed in the inbox, including hand-dropped files."""
        return [await _process(engine, rel) for rel in engine.ingest.pending_inbox()]

    @app.get("/api/progress")
    async def progress_stream() -> StreamingResponse:
        """Live ingest steps, so a slow document reads as working rather than hung."""
        return StreamingResponse(
            stream(app.state.progress),
            media_type="text/event-stream",
            # x-accel-buffering: a reverse proxy would otherwise hold the stream until it ends.
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    async def _process(engine: Bismuth, rel: PurePosixPath) -> IngestOut:
        bus: ProgressBus = app.state.progress
        try:
            result = await engine.ingest.process(rel, on_progress=bus.publish)
        except BismuthError as exc:
            bus.publish(Progress(stage=Stage.FAILED, filename=rel.name, note=str(exc)))
            return IngestOut(filename=rel.name, ok=False, reason=str(exc))
        return _result_of(result)

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
        return {"path": result.path, "files": result.files}

    @app.post("/api/delete-many")
    async def delete_many(body: DeleteManyIn, engine: Engine) -> dict[str, Any]:
        """Delete several documents in one reversible batch."""
        try:
            result = await engine.deletion.delete_files([PurePosixPath(p) for p in body.paths])
        except BismuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"files": result.files}

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
        return FileResponse(STATIC / "index.html")

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


class SetupStateOut(BaseModel):
    configured: bool
    providers: list[dict[str, Any]]
    provider_id: str = ""
    api_key_tail: str = ""
    api_base: str | None = None
    model_fast: str = ""
    model_reasoning: str = ""
    vault_path: str = ""


class ProviderCheckIn(BaseModel):
    provider_id: str
    api_key: str = ""
    api_base: str | None = None
    reuse_saved_key: bool = False


class ProviderCheckOut(BaseModel):
    ok: bool
    error: str = ""
    models: list[str] = []
    suggested_fast: str = ""
    suggested_reasoning: str = ""


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
    model_fast: str
    model_reasoning: str
    vault_path: str


def _result_of(result: IngestResult) -> IngestOut:
    return IngestOut(
        filename=result.filename,
        ok=True,
        document_id=result.document_id,
        destination=str(result.destination),
        placed=result.placement.is_placed,
        created_folder=result.placement.created_folder,
        reason=result.placement.rationale,
        duplicate=result.duplicate,
    )
