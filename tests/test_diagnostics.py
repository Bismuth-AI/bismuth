"""Diagnostic API over run-scoped model and pipeline logs."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bismuth.api import diagnostics
from bismuth.logging_setup import (
    configure_logging,
    log_context,
    log_llm_call,
    log_trace,
    update_run_manifest,
)


@pytest.fixture
def run_client(tmp_path: Path) -> tuple[TestClient, str, str]:
    diagnostics._INDEX.clear()
    logs = configure_logging(log_dir=tmp_path / "logs")
    update_run_manifest(model="test-model", vault_path=str(tmp_path / "vault"))
    log_trace("document.read", document_id="doc-1", filename="report.pdf", parse_ms=12)
    with log_context(
        stage="card.read",
        document_id="doc-1",
        window_id="doc-1:window-001",
        folder="Research",
    ):
        call_id = log_llm_call(
            {
                "model": "test-model",
                "schema": "CardUpdate",
                "system": "Summarize documents.",
                "user": "Read the orbital research report.",
                "attempts": [
                    {
                        "n": 1,
                        "ms": 25,
                        "stream": {
                            "content": "Orbital research summary",
                            "reasoning_content": "",
                            "finish_reason": "stop",
                            "completed": True,
                        },
                    }
                ],
                "ok": True,
                "ms": 25,
            }
        )
    for name in ("bismuth.llm", "bismuth.trace"):
        for handler in logging.getLogger(name).handlers:
            handler.flush()

    latest = diagnostics._read(logs / "latest.json")
    app = FastAPI()
    app.include_router(diagnostics.router)
    return TestClient(app), str(latest["run_id"]), call_id


def test_lists_runs_and_call_metadata(run_client: tuple[TestClient, str, str]) -> None:
    client, run_id, call_id = run_client

    runs = client.get("/api/runs").json()
    calls = client.get(f"/api/runs/{run_id}/calls").json()

    assert runs[0]["run_id"] == run_id
    assert runs[0]["model"] == "test-model"
    assert runs[0]["calls"] == 1
    assert calls["calls"][0]["call_id"] == call_id
    assert calls["calls"][0]["filename"] == "report.pdf"


def test_filters_pipeline_events(run_client: tuple[TestClient, str, str]) -> None:
    client, run_id, _ = run_client

    events = client.get(
        f"/api/runs/{run_id}/events",
        params={"event": "document.read", "document_id": "doc-1"},
    ).json()

    assert len(events) == 1
    assert events[0]["filename"] == "report.pdf"


def test_searches_request_and_response_text(run_client: tuple[TestClient, str, str]) -> None:
    client, run_id, call_id = run_client

    request_hit = client.get(f"/api/runs/{run_id}/search", params={"q": "orbital"}).json()
    no_hit = client.get(f"/api/runs/{run_id}/search", params={"q": ""}).json()

    assert request_hit["hits"][0]["call_id"] == call_id
    assert request_hit["hits"][0]["where"] == ["input", "output"]
    assert no_hit["hits"] == []


def test_returns_one_exact_call(run_client: tuple[TestClient, str, str]) -> None:
    client, run_id, call_id = run_client

    call = client.get(f"/api/runs/{run_id}/calls/{call_id}").json()

    assert call["system"] == "Summarize documents."
    assert call["user"] == "Read the orbital research report."
    assert call["attempts"][0]["content"] == "Orbital research summary"
    assert call["filename"] == "report.pdf"


@pytest.mark.parametrize("run_id", ["bad$id", "missing-run"])
def test_rejects_bad_or_missing_runs(run_client: tuple[TestClient, str, str], run_id: str) -> None:
    client, _, _ = run_client

    response = client.get(f"/api/runs/{run_id}/calls")

    assert response.status_code in {400, 404}


def test_rejects_bad_or_missing_call_ids(run_client: tuple[TestClient, str, str]) -> None:
    client, run_id, _ = run_client

    assert client.get(f"/api/runs/{run_id}/calls/bad$id").status_code == 400
    assert client.get(f"/api/runs/{run_id}/calls/missing-call").status_code == 404
