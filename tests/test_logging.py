"""Logging: text to bismuth.log, structured LLM traffic to llm.jsonl."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from bismuth.logging_setup import configure_logging, log_llm_call


def test_logs_go_to_the_given_dir(tmp_path: Path) -> None:
    logs = configure_logging(log_dir=tmp_path / "logs")
    assert logs == (tmp_path / "logs").resolve()
    logging.getLogger("bismuth").info("hello")
    for h in logging.getLogger("bismuth").handlers:
        h.flush()
    assert "hello" in (logs / "bismuth.log").read_text(encoding="utf-8")


def test_files_are_truncated_on_each_run(tmp_path: Path) -> None:
    logs = configure_logging(log_dir=tmp_path / "logs")
    logging.getLogger("bismuth").warning("from the first run")
    for h in logging.getLogger("bismuth").handlers:
        h.flush()
    assert "first run" in (logs / "bismuth.log").read_text(encoding="utf-8")

    configure_logging(log_dir=tmp_path / "logs")
    assert "first run" not in (logs / "bismuth.log").read_text(encoding="utf-8")


def test_an_llm_call_is_one_json_line_with_prompt_and_response(tmp_path: Path) -> None:
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
    assert rec["user"] == "아폴로 계약서를 분류해줘"
    assert rec["attempts"][0]["raw"] == '{"folder": "아폴로/2023"}'
    assert rec["ok"] is True


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
    assert len(text) == 1
    assert len(jsonl) == 1
