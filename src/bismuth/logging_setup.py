"""Logging setup: text log + JSONL of LLM calls and pipeline traces under ``logs/``, truncated on each start."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

LOG_DIR = Path("logs")

LLM_LOGGER = "bismuth.llm"

TRACE_LOGGER = "bismuth.trace"
"""Pipeline decisions, one JSON object per line. Written for a machine to replay:
every line carries ``event`` and ``document_id``, so filtering by document
reconstructs the whole run without reading prose."""


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
    for logger in (root, logging.getLogger(LLM_LOGGER), logging.getLogger(TRACE_LOGGER)):
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
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

    # These do not propagate, so their lines don't also land in bismuth.log or the console.
    _attach_jsonl(LLM_LOGGER, logs / "llm.jsonl")
    _attach_jsonl(TRACE_LOGGER, logs / "trace.jsonl")

    logging.getLogger("bismuth").info("logging to %s (truncated on start)", logs)
    return logs


def _attach_jsonl(logger_name: str, path: Path) -> None:
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_JsonlFormatter())
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False


def log_llm_call(record: dict[str, Any]) -> None:
    """Append one model call as a JSON line. Called by the LLM adapter."""
    logging.getLogger(LLM_LOGGER).debug("llm", extra={"record": record})


def log_trace(event: str, **fields: Any) -> None:
    """Append one pipeline decision as a JSON line.

    Every call must be reconstructible on its own: pass the ids, the inputs and the
    outcome, not a sentence about them.
    """
    logging.getLogger(TRACE_LOGGER).debug(event, extra={"record": {"event": event, **fields}})
