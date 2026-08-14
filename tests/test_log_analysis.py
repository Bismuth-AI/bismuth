"""The compact run index must be sufficient for routine analysis without raw chunks."""

from __future__ import annotations

import logging
from pathlib import Path

from scripts.inspect_run import main as inspect_run
from scripts.where_the_time_went import main as time_report

from bismuth.logging_setup import configure_logging, log_context, log_llm_call, log_trace


def test_time_report_includes_agent_calls_and_maintenance_windows(
    tmp_path: Path, capsys
) -> None:
    logs = configure_logging(log_dir=tmp_path / "logs")
    window_id = "batch:test:window-001"
    log_trace(
        "maintenance.window_started",
        workflow_id="batch:test",
        window_id=window_id,
        document_ids=["d1", "d2"],
    )
    with log_context(
        workflow_id="batch:test",
        window_id=window_id,
        agent_run_id="agent-test",
        stage="planner",
    ):
        log_llm_call(
            {
                "operation": "agent_chat",
                "messages": [{"role": "user", "content": "inspect"}],
                "stream": {
                    "chunks": [{"n": 1, "raw": {"delta": "done"}}],
                    "content": "done",
                    "completed": True,
                    "elapsed_ms": 750,
                    "usage": {"input_tokens": 20, "output_tokens": 4},
                },
            }
        )
    log_trace(
        "maintenance.window_finished",
        workflow_id="batch:test",
        window_id=window_id,
        moved=2,
        status="done",
    )
    for name in ("bismuth.llm", "bismuth.trace"):
        for handler in logging.getLogger(name).handlers:
            handler.flush()

    assert time_report(["where_the_time_went.py", str(logs)]) == 0
    output = capsys.readouterr().out
    assert "구조 유지보수 1개 창" in output
    assert "LLM calls: 1" in output
    assert "planner" in output

    assert inspect_run([str(logs)]) == 0
    overview = capsys.readouterr().out
    assert '"windows"' in overview
    assert window_id in overview

    call_id = next((logs / "runs").glob("*/calls/*.request.json")).stem.removesuffix(
        ".request"
    )
    assert inspect_run([str(logs), "--call", call_id]) == 0
    call = capsys.readouterr().out
    assert '"request"' in call
    assert '"response"' in call
    assert "inspect" in call
