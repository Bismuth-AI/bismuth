"""The compact run index must be sufficient for routine analysis without raw chunks.

Reading hundreds of thousands of provider chunks to answer "which stage was slow" or
"what did the model actually see" is the thing the split was made to avoid.
"""

from __future__ import annotations

import logging
from pathlib import Path

from scripts.inspect_run import main as inspect_run
from scripts.where_the_time_went import main as time_report

from bismuth.logging_setup import configure_logging, log_context, log_llm_call, log_trace


def _flush() -> None:
    for name in ("bismuth.llm", "bismuth.trace"):
        for handler in logging.getLogger(name).handlers:
            handler.flush()


def _a_run(logs_dir: Path) -> Path:
    logs = configure_logging(log_dir=logs_dir)
    log_trace("document.read", document_id="d1", filename="a.pdf", parse_ms=400, card_ms=1_600)
    with log_context(stage="card.update", window_id="d1:window-002"):
        log_llm_call({"schema": "CardUpdate", "call": "#1", "ms": 900, "attempts": [{"n": 1}]})
    with log_context(stage="subdivision.review", window_id="review:docs-001"):
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
        "document.filed",
        document_id="d1",
        filename="a.pdf",
        total_ms=3_000,
        place_ms=800,
        subdivide_ms=1_200,
    )
    _flush()
    return logs


def test_the_time_report_counts_agent_calls_and_reads_tokens_from_artifacts(
    tmp_path: Path, capsys
) -> None:
    """Leaving agent calls out does not make the total incomplete, it makes it wrong."""
    logs = _a_run(tmp_path / "logs")

    assert time_report(["where_the_time_went.py", str(logs)]) == 0
    output = capsys.readouterr().out

    assert "문서 1건" in output
    assert "CardUpdate" in output
    assert "agent_chat" in output  # the agent call is in the table
    assert "20" in output and "4" in output  # usage pulled from the response artifact


def test_the_overview_lists_stages_and_windows(tmp_path: Path, capsys) -> None:
    logs = _a_run(tmp_path / "logs")

    assert inspect_run([str(logs)]) == 0
    overview = capsys.readouterr().out

    assert "subdivision.review" in overview
    assert "review:docs-001" in overview
    assert "d1:window-002" in overview


def test_a_document_id_reconstructs_that_document_alone(tmp_path: Path, capsys) -> None:
    logs = _a_run(tmp_path / "logs")

    assert inspect_run([str(logs), "--document", "d1"]) == 0
    selected = capsys.readouterr().out.splitlines()

    assert selected  # every line of this run belongs to d1
    assert all('"document_id": "d1"' in line for line in selected)


def test_a_call_id_opens_the_exact_request_and_response(tmp_path: Path, capsys) -> None:
    logs = _a_run(tmp_path / "logs")

    call_id = next((logs / "runs").glob("*/calls/*.request.json")).stem.removesuffix(".request")
    assert inspect_run([str(logs), "--call", call_id]) == 0
    call = capsys.readouterr().out

    assert '"request"' in call
    assert '"response"' in call
