"""Logging has to survive the server starting.

uvicorn configures logging with ``dictConfig``, which closes every handler that already
existed. Ours stayed attached and stayed enabled, so nothing raised -- and nothing was
written. A server that ingested thirty-three documents left two lines in bismuth.log and
empty trace and llm files, which is exactly the case where those files are wanted.
"""

from __future__ import annotations

import json
import logging
import logging.config
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.api.app import create_app
from bismuth.config import Settings
from bismuth.container import build
from bismuth.logging_setup import configure_logging, log_llm_call, log_trace


@pytest.fixture(autouse=True)
def _restore_logging():
    """Leave the process's logging as it was found; these tests reconfigure it."""
    yield
    for name in ("bismuth", "bismuth.llm", "bismuth.trace"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_our_handlers_are_closed_by_a_dictconfig(tmp_path: Path) -> None:
    """The mechanism, pinned: not disabled, not detached -- closed, and silent."""
    logs = tmp_path / "logs"
    configure_logging(log_dir=logs)
    logging.getLogger("bismuth.test").info("before")

    logging.config.dictConfig(
        {"version": 1, "disable_existing_loggers": False, "handlers": {}, "loggers": {}}
    )
    logging.getLogger("bismuth.test").info("after")

    written = (logs / "bismuth.log").read_text(encoding="utf-8")
    assert "before" in written
    assert "after" not in written  # silent, and that is the whole problem


def test_the_server_logs_once_it_is_serving(tmp_path: Path, monkeypatch) -> None:
    """The regression: configure after uvicorn, not before."""
    monkeypatch.chdir(tmp_path)  # LOG_DIR is relative; keep it out of the repo
    settings = Settings(vault_path=tmp_path / "vault")
    app = create_app(settings)
    app.state.engine = build(settings, llm=FakeLLM(handler=lambda *_: None))

    with TestClient(app):
        # Whatever the server does from here has to be recoverable from the logs.
        logging.config.dictConfig(
            {"version": 1, "disable_existing_loggers": False, "handlers": {}, "loggers": {}}
        )
        configure_logging()  # what startup does, after uvicorn's turn
        logging.getLogger("bismuth.services.cards").info("serving")
        log_trace("card.done", document_id="d1")
        log_llm_call({"call": "#1"})

    logs = tmp_path / "logs"
    assert "serving" in (logs / "bismuth.log").read_text(encoding="utf-8")
    assert json.loads((logs / "trace.jsonl").read_text(encoding="utf-8").strip())["event"] == (
        "card.done"
    )
    assert (logs / "llm.jsonl").read_text(encoding="utf-8").strip()
