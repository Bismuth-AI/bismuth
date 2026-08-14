"""The structure pass can be resumed without re-ingesting durable documents."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bismuth.api.maintenance import (
    MaintenanceState,
    MaintenanceStatus,
    load,
    recover_interrupted,
    save,
)
from bismuth.api.workflows.ingestion import _next_deferred, _work_candidates


def test_checkpoint_round_trips_in_the_vault(tmp_path: Path) -> None:
    state = MaintenanceState(
        status="failed",
        source="batch:abc",
        error="tool calling is disabled",
        attempts=1,
        reviewed_scope_fingerprints={"과학기술": "abc123"},
    )

    save(tmp_path, state)

    assert load(tmp_path) == state
    assert (tmp_path / ".bismuth" / "maintenance.json").is_file()


@pytest.mark.parametrize("status", ["pending", "running"])
def test_restart_turns_an_interrupted_pass_into_a_retryable_failure(
    tmp_path: Path, status: MaintenanceStatus
) -> None:
    save(tmp_path, MaintenanceState(status=status, attempts=2))

    recovered = recover_interrupted(tmp_path)

    assert recovered.status == "failed"
    assert recovered.attempts == 2
    assert "retried" in recovered.error
    assert load(tmp_path) == recovered


def test_legacy_empty_success_is_recovered_as_an_incomplete_plan(tmp_path: Path) -> None:
    save(tmp_path, MaintenanceState(status="done", attempts=2))

    recovered = recover_interrupted(tmp_path)

    assert recovered.status == "failed"
    assert "without submitting" in recovered.error


def test_waiting_arrivals_survive_restart_without_becoming_a_failure(tmp_path: Path) -> None:
    waiting = MaintenanceState(status="waiting", pending_document_ids=["a", "b"])
    save(tmp_path, waiting)

    recovered = recover_interrupted(tmp_path)

    assert recovered == waiting


def test_partial_loose_documents_remain_retryable_after_restart(tmp_path: Path) -> None:
    partial = MaintenanceState(
        status="partial",
        deferred_document_ids=["a", "b"],
        review_round=2,
    )
    save(tmp_path, partial)

    recovered = recover_interrupted(tmp_path)

    assert recovered == partial


def test_selected_deferred_documents_leave_checkpoint_only_after_resolution() -> None:
    assert _next_deferred(["old-a", "old-b"], ["new", "old-a"], ()) == ["old-b"]
    assert _next_deferred(
        ["old-a", "old-b"], ["new", "old-a"], ("old-a", "new")
    ) == ["old-b", "old-a", "new"]


def test_new_arrivals_do_not_replay_deferred_documents_implicitly() -> None:
    catalog = Mock()
    catalog.load_card.side_effect = lambda document_id: (
        object() if document_id != "missing" else None
    )
    engine = SimpleNamespace(catalog=catalog)
    state = MaintenanceState(
        pending_document_ids=["new-a", "new-b"],
        deferred_document_ids=["old-a", "missing"],
    )

    assert _work_candidates(engine, state) == ["new-a", "new-b"]
    assert _work_candidates(engine, state, exclude={"new-a", "old-a"}) == ["new-b"]
