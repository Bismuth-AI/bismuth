"""The structure pass can be resumed without re-ingesting durable documents."""

from __future__ import annotations

from pathlib import Path

import pytest

from bismuth.api.maintenance import (
    MaintenanceState,
    MaintenanceStatus,
    load,
    recover_interrupted,
    save,
)


def test_checkpoint_round_trips_in_the_vault(tmp_path: Path) -> None:
    state = MaintenanceState(
        status="failed",
        source="batch:abc",
        error="tool calling is disabled",
        attempts=1,
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
