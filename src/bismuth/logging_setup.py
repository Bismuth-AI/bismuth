"""Logging setup: text log + JSONL of LLM calls under ``logs/``, truncated on each start."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

LOG_DIR = Path("logs")

LLM_LOGGER = "bismuth.llm"


class _JsonlFormatter(logging.Formatter):
    """Formats each record as one JSON line from ``extra["record"]``, falling back to the plain message."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = getattr(record, "record", None) or {
            "message": record.getMessage()
        }
        try:
            return json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            return json.dumps({"message": str(payload)}, ensure_ascii=False)


def configure_logging(*, verbose: bool = False, log_dir: Path | None = None) -> Path:
    """Point logging at ``./logs`` (text + JSONL), truncating the files. Idempotent."""
    logs = (log_dir or LOG_DIR).resolve()
    logs.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("bismuth")
    # Must not stack handlers, or lines get logged multiple times.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    llm = logging.getLogger(LLM_LOGGER)
    for handler in list(llm.handlers):
        llm.removeHandler(handler)
        handler.close()

    root.setLevel(logging.DEBUG)
    root.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    # mode="w": truncate on startup, so each run is a clean file.
    text = logging.FileHandler(logs / "bismuth.log", mode="w", encoding="utf-8")
    text.setLevel(logging.DEBUG)
    text.setFormatter(fmt)
    root.addHandler(text)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)

    # Does not propagate, so it doesn't also land in bismuth.log or the console.
    jsonl = logging.FileHandler(logs / "llm.jsonl", mode="w", encoding="utf-8")
    jsonl.setLevel(logging.DEBUG)
    jsonl.setFormatter(_JsonlFormatter())
    llm.addHandler(jsonl)
    llm.setLevel(logging.DEBUG)
    llm.propagate = False

    logging.getLogger("bismuth").info("logging to %s (truncated on start)", logs)
    return logs


def log_llm_call(record: dict[str, Any]) -> None:
    """Append one model call as a JSON line. Called by the LLM adapter."""
    logging.getLogger(LLM_LOGGER).debug("llm", extra={"record": record})
