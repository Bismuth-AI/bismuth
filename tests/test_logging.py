"""Logging: text to bismuth.log, a compact call index to llm.jsonl, exact evidence per run.

The top-level files are a view of the current run. What has to survive a restart -- and
what is too large to read inline -- lives under ``logs/runs/<run_id>/``.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

from bismuth.logging_setup import configure_logging, log_context, log_llm_call, log_trace


def test_logs_go_to_the_given_dir(tmp_path: Path) -> None:
    logs = configure_logging(log_dir=tmp_path / "logs")
    assert logs == (tmp_path / "logs").resolve()
    logging.getLogger("bismuth").info("hello")
    for h in logging.getLogger("bismuth").handlers:
        h.flush()
    assert "hello" in (logs / "bismuth.log").read_text(encoding="utf-8")


def test_current_files_are_truncated_but_prior_run_is_retained(tmp_path: Path) -> None:
    """The old contract lost the previous run on restart, which is when it was wanted."""
    logs = configure_logging(log_dir=tmp_path / "logs")
    first = json.loads((logs / "latest.json").read_text(encoding="utf-8"))
    logging.getLogger("bismuth").warning("from the first run")
    for h in logging.getLogger("bismuth").handlers:
        h.flush()
    assert "first run" in (logs / "bismuth.log").read_text(encoding="utf-8")

    configure_logging(log_dir=tmp_path / "logs")
    assert "first run" not in (logs / "bismuth.log").read_text(encoding="utf-8")
    assert "first run" in (logs / first["path"] / "bismuth.log").read_text(encoding="utf-8")


def test_an_llm_call_has_a_compact_index_and_exact_artifacts(tmp_path: Path) -> None:
    logs = configure_logging(log_dir=tmp_path / "logs")
    log_llm_call(
        {
            "call": "#1",
            "model": "ollama/qwen3:8b",
            "schema": "PlacementDecision",
            "system": "너는 사서다",
            "user": "아폴로 계약서를 분류해줘",
            "attempts": [
                {"n": 1, "raw": '{"folder": "아폴로/2023"}', "in_tokens": 120, "out_tokens": 8}
            ],
            "ok": True,
        }
    )
    for h in logging.getLogger("bismuth.llm").handlers:
        h.flush()

    lines = (logs / "llm.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    request = json.loads((logs / rec["request_ref"]).read_text(encoding="utf-8"))
    response = json.loads((logs / rec["response_ref"]).read_text(encoding="utf-8"))
    assert request["user"] == "아폴로 계약서를 분류해줘"
    assert response["attempts"][0]["raw"] == '{"folder": "아폴로/2023"}'
    assert rec["ok"] is True
    assert rec["schema_version"] == 1
    assert rec["run_id"]
    assert rec["call_id"]


def test_the_manifest_records_what_the_run_was_configured_with(tmp_path: Path) -> None:
    """A conclusion about a model or a setting is unverifiable if the run never said which."""
    logs = configure_logging(log_dir=tmp_path / "logs")
    latest = json.loads((logs / "latest.json").read_text(encoding="utf-8"))
    manifest = json.loads((logs / latest["path"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == latest["run_id"]
    assert manifest["status"] == "running"
    assert manifest["bismuth_version"]


def test_llm_traffic_does_not_leak_into_bismuth_log(tmp_path: Path) -> None:
    logs = configure_logging(log_dir=tmp_path / "logs")
    log_llm_call({"call": "#1", "user": "secret prompt text"})
    for h in logging.getLogger("bismuth.llm").handlers:
        h.flush()
    assert "secret prompt text" not in (logs / "bismuth.log").read_text(encoding="utf-8")


def test_reconfiguring_does_not_duplicate_handlers(tmp_path: Path) -> None:
    for _ in range(3):
        configure_logging(log_dir=tmp_path / "logs")
    text = [h for h in logging.getLogger("bismuth").handlers if isinstance(h, logging.FileHandler)]
    jsonl = [
        h for h in logging.getLogger("bismuth.llm").handlers if isinstance(h, logging.FileHandler)
    ]
    assert len(text) == 2  # current view + durable run copy
    assert len(jsonl) == 2


def test_server_reopens_the_cli_run_instead_of_creating_an_empty_duplicate(
    tmp_path: Path,
) -> None:
    """The CLI starts the run; the server reconfigures after uvicorn and must not orphan it."""
    logs = configure_logging(log_dir=tmp_path / "logs")
    first = json.loads((logs / "latest.json").read_text(encoding="utf-8"))
    logging.getLogger("bismuth").info("before reopen")

    configure_logging(log_dir=logs, continue_active_run=True)
    second = json.loads((logs / "latest.json").read_text(encoding="utf-8"))
    logging.getLogger("bismuth").info("after reopen")
    for handler in logging.getLogger("bismuth").handlers:
        handler.flush()

    assert second["run_id"] == first["run_id"]
    assert len(list((logs / "runs").iterdir())) == 1
    durable = (logs / first["path"] / "bismuth.log").read_text(encoding="utf-8")
    assert "before reopen" in durable
    assert "after reopen" in durable


def test_raw_chunks_are_detached_and_compressed(tmp_path: Path) -> None:
    """One run put 129.8 MB into llm.jsonl, with single lines of 8.32 MB."""
    logs = configure_logging(log_dir=tmp_path / "logs")
    log_llm_call(
        {
            "operation": "agent_chat",
            "messages": [{"role": "user", "content": "inspect"}],
            "stream": {
                "chunks": [{"n": 1, "raw": {"delta": "x"}}],
                "content": "x",
                "completed": True,
            },
        }
    )
    for handler in logging.getLogger("bismuth.llm").handlers:
        handler.flush()
    record = json.loads((logs / "llm.jsonl").read_text(encoding="utf-8"))
    assert record["raw_chunks"] == 1
    assert (logs / "llm.jsonl").stat().st_size < 2_000
    with gzip.open(logs / record["raw_stream_ref"], "rt", encoding="utf-8") as stream:
        chunk = json.loads(stream.readline())
    assert chunk["raw"] == {"delta": "x"}


def test_timeline_joins_stage_call_and_full_tool_result(tmp_path: Path) -> None:
    logs = configure_logging(log_dir=tmp_path / "logs")
    with log_context(
        stage="subdivision.review",
        window_id="review:docs-001",
        agent_run_id="agent-test",
    ):
        call_id = log_llm_call({"operation": "agent_chat", "messages": []})
        log_trace(
            "agent.tool_result",
            llm_call_id=call_id,
            id="tool-call-1",
            name="inventory",
            content="전체 결과 " * 200,
        )
    for handler in logging.getLogger("bismuth.trace").handlers:
        handler.flush()

    events = [
        json.loads(line) for line in (logs / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    tool = next(event for event in events if event["event"] == "agent.tool_result")
    assert tool["run_id"]
    assert tool["stage"] == "subdivision.review"
    assert tool["window_id"] == "review:docs-001"
    assert tool["agent_run_id"] == "agent-test"
    assert tool["llm_call_id"] == call_id
    assert tool["tool_call_id"] == "tool-call-1"
    # The timeline keeps a preview; the artifact keeps what the model actually saw.
    assert len(tool["preview"]) <= 200
    artifact = json.loads((logs / tool["result_ref"]).read_text(encoding="utf-8"))
    assert artifact["content"] == "전체 결과 " * 200
