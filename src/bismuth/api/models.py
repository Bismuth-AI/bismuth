"""Pydantic request and response models for the HTTP API."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from bismuth.api.maintenance import MaintenanceState
from bismuth.container import Bismuth
from bismuth.domain.document import sidecar_name
from bismuth.ports.llm import Spend
from bismuth.services.ingest import IngestResult
from bismuth.services.sidecar import read_sidecar_meta


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
    error: str = ""
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
    deferred_documents: int
    completed_windows: int
    review_round: int
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
            deferred_documents=len(state.deferred_document_ids),
            completed_windows=state.completed_windows,
            review_round=state.review_round,
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


def result_of(result: IngestResult, *, spend: Spend) -> IngestOut:
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
