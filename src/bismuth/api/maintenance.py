"""Durable state for the autonomous library-maintenance stage.

Ingested documents and their cards are already durable before maintenance starts.  This
small checkpoint lets a user fix or switch the model and retry only that final stage,
including after a browser refresh or server restart.
"""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from bismuth.ports.vault import STATE_DIR

MaintenanceStatus = Literal["idle", "waiting", "pending", "running", "done", "failed"]
_FILENAME = "maintenance.json"


class MaintenanceState(BaseModel):
    status: MaintenanceStatus = "idle"
    source: str = ""
    error: str = ""
    summary: str = ""
    attempts: int = 0
    moved: int = 0
    applied: bool = False
    pending_document_ids: list[str] = Field(default_factory=list)
    completed_windows: int = 0
    current_window_documents: int = 0
    started_at: float | None = None
    finished_at: float | None = None


def load(root: Path) -> MaintenanceState:
    path = _path(root)
    if not path.exists():
        return MaintenanceState()
    try:
        return MaintenanceState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MaintenanceState(
            status="failed",
            error="The saved maintenance checkpoint could not be read. You can safely retry.",
            finished_at=time.time(),
        )


def save(root: Path, state: MaintenanceState) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".maintenance-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(state.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def recover_interrupted(root: Path) -> MaintenanceState:
    state = load(root)
    if state.status in {"pending", "running"}:
        state = state.model_copy(
            update={
                "status": "failed",
                "error": "The server stopped while organizing. The saved documents can be retried.",
                "finished_at": time.time(),
            }
        )
        save(root, state)
    elif state.status == "done" and not state.applied and not state.summary.strip():
        # Older builds treated an agent max-turn stop (no submitted plan and no final
        # explanation) as a successful no-op. Preserve the evidence but make it retryable.
        state = state.model_copy(
            update={
                "status": "failed",
                "error": (
                    "The earlier planner stopped without submitting a structure plan. "
                    "The saved documents can be retried."
                ),
                "finished_at": time.time(),
            }
        )
        save(root, state)
    return state


def _path(root: Path) -> Path:
    return root / STATE_DIR / _FILENAME
