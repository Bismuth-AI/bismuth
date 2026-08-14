"""Run-scoped, joinable diagnostics for model traffic and pipeline decisions.

The top-level files in ``logs/`` are a compact view of the current run. Durable,
potentially large evidence lives below ``logs/runs/<run_id>/`` so a debugger can read a
small timeline first and open exact request, response, tool-result, or raw-stream
artifacts only when needed.

One run put 129.8 MB into a single ``llm.jsonl`` with lines as large as 8.32 MB, because
provider chunks, repeated prompts and tool schemas all shared one record. Splitting them
is what makes the index readable; keeping them is what makes a cause provable.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import logging
import platform
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bismuth import __version__
from bismuth.ports.llm import CURRENT_DOCUMENT

LOG_DIR = Path("logs")
SCHEMA_VERSION = 1

LLM_LOGGER = "bismuth.llm"

TRACE_LOGGER = "bismuth.trace"
"""Pipeline decisions, one JSON object per line. Written for a machine to replay:
every line carries ``event`` and ``document_id``, so filtering by document
reconstructs the whole run without reading prose."""

_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("bismuth_log_context", default=None)
_WRITE_LOCK = threading.RLock()
_ACTIVE_LOG_ROOT: Path | None = None
_ACTIVE_RUN_DIR: Path | None = None
_ACTIVE_RUN_ID = ""


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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{uuid.uuid4().hex[:10]}"


def current_log_context() -> dict[str, Any]:
    """Return the explicit execution identity inherited by this async task."""
    return dict(_CONTEXT.get() or {})


@contextmanager
def log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    """Attach stage/call identity to every log written inside one execution scope.

    A ContextVar rather than an argument: the identity has to reach the LLM adapter,
    which is several layers below whoever knows which stage is running and must not
    learn about stages to say so.
    """
    merged = current_log_context()
    merged.update({key: value for key, value in fields.items() if value is not None})
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)


def _envelope(**extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": _ACTIVE_RUN_ID,
        **current_log_context(),
        **extra,
    }
    record.setdefault("document_id", CURRENT_DOCUMENT.get(""))
    return record


def active_run_dir() -> Path | None:
    """The directory holding this run's durable evidence, if logging has been configured."""
    return _ACTIVE_RUN_DIR


def configure_logging(
    *,
    verbose: bool = False,
    log_dir: Path | None = None,
    continue_active_run: bool = False,
) -> Path:
    """Start one diagnostic run: compact current-run files plus a durable run directory.

    ``continue_active_run`` reopens the run this process already started instead of
    creating a second, near-empty one -- the server configures logging twice (once at
    import, once after uvicorn has installed its own handlers).
    """
    global _ACTIVE_LOG_ROOT, _ACTIVE_RUN_DIR, _ACTIVE_RUN_ID

    logs = (log_dir or LOG_DIR).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    reuse = bool(
        continue_active_run
        and logs == _ACTIVE_LOG_ROOT
        and _ACTIVE_RUN_DIR is not None
        and _ACTIVE_RUN_DIR.exists()
        and _ACTIVE_RUN_ID
    )
    run_id = _ACTIVE_RUN_ID if reuse else _new_run_id()
    run_dir = _ACTIVE_RUN_DIR if reuse else logs / "runs" / run_id
    assert run_dir is not None
    for child in ("calls", "streams", "tools"):
        (run_dir / child).mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("bismuth")
    # Must not stack handlers, or lines get logged multiple times.
    for logger in (root, logging.getLogger(LLM_LOGGER), logging.getLogger(TRACE_LOGGER)):
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    _ACTIVE_LOG_ROOT = logs
    _ACTIVE_RUN_DIR = run_dir
    _ACTIVE_RUN_ID = run_id
    _CONTEXT.set({})

    root.setLevel(logging.DEBUG)
    root.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    # mode="w" for the top-level view: each run starts clean. The run copy is append,
    # so reopening after uvicorn does not discard what the CLI already wrote.
    for path, mode in (
        (logs / "bismuth.log", "w"),
        (run_dir / "bismuth.log", "a" if reuse else "w"),
    ):
        handler = logging.FileHandler(path, mode=mode, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(fmt)
        root.addHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)

    # These do not propagate, so their lines don't also land in bismuth.log or the console.
    _attach_jsonl(
        LLM_LOGGER,
        (logs / "llm.jsonl", "w"),
        (run_dir / "llm.jsonl", "a" if reuse else "w"),
    )
    _attach_jsonl(
        TRACE_LOGGER,
        (logs / "trace.jsonl", "w"),
        (run_dir / "timeline.jsonl", "a" if reuse else "w"),
    )

    if reuse:
        update_run_manifest(logging_reopened_at=_now())
        root.info("reopened logging run %s at %s", run_id, run_dir)
        return logs

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "started_at": _now(),
        "bismuth_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "paths": {
            "timeline": "timeline.jsonl",
            "llm_index": "llm.jsonl",
            "text_log": "bismuth.log",
            "calls": "calls",
            "streams": "streams",
            "tools": "tools",
        },
    }
    _write_json(run_dir / "manifest.json", manifest)
    _write_json(logs / "latest.json", {"run_id": run_id, "path": f"runs/{run_id}"})
    root.info("logging run %s to %s", run_id, run_dir)
    return logs


def _attach_jsonl(logger_name: str, *targets: tuple[Path, str]) -> None:
    logger = logging.getLogger(logger_name)
    # Uvicorn's dictConfig can disable named loggers that are not in its own table.
    # Re-enabling only our explicitly configured sinks keeps raw call/trace logs alive
    # when Bismuth is launched through the web server.
    logger.disabled = False
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for path, mode in targets:
        handler = logging.FileHandler(path, mode=mode, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(_JsonlFormatter())
        logger.addHandler(handler)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def update_run_manifest(**fields: Any) -> None:
    """Add non-secret runtime facts after the application has resolved its settings.

    Without the model and generation settings in the manifest, any conclusion about
    them is unverifiable after the fact.
    """
    run_dir = _ACTIVE_RUN_DIR
    if run_dir is None:
        return
    path = run_dir / "manifest.json"
    with _WRITE_LOCK:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest.update(fields)
        _write_json(path, manifest)


def finish_run_manifest() -> None:
    """Mark a clean application shutdown without deleting any diagnostic evidence."""
    update_run_manifest(finished_at=_now(), status="finished")


def _artifact_ref(path: Path) -> str:
    root = _ACTIVE_LOG_ROOT
    return path.relative_to(root).as_posix() if root is not None else str(path)


def _without_keys(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in keys}


def _split_llm_record(
    record: dict[str, Any], call_id: str
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Create exact request/response artifacts and detach provider stream chunks."""
    source = copy.deepcopy(record)
    request: dict[str, Any] = {
        **_envelope(call_id=call_id),
        "t": source.get("t") or _now(),
        "operation": source.get("operation") or "structured",
        "model": source.get("model"),
        "schema": source.get("schema"),
        "call": source.get("call"),
    }
    for key in (
        "system",
        "user",
        "messages",
        "tools",
        "allowed",
        "parameters",
        "max_tokens",
        "native_schema",
    ):
        if key in source:
            request[key] = source[key]

    attempts = list(source.get("attempts") or [])
    request_attempts: list[dict[str, Any]] = []
    response_attempts: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    for attempt in attempts:
        request_attempts.append(
            {
                key: attempt[key]
                for key in ("n", "messages", "max_tokens", "request_parameters")
                if key in attempt
            }
        )
        response_attempt = _without_keys(attempt, "messages", "request_parameters")
        stream = response_attempt.get("stream")
        if isinstance(stream, dict):
            for chunk in stream.pop("chunks", []) or []:
                chunks.append({"attempt": attempt.get("n"), **chunk})
        response_attempts.append(response_attempt)
    if request_attempts:
        request["attempts"] = request_attempts

    top_stream = source.get("stream")
    if isinstance(top_stream, dict):
        for chunk in top_stream.pop("chunks", []) or []:
            chunks.append({"attempt": None, **chunk})

    response = {
        **_envelope(call_id=call_id),
        "t": source.get("t") or _now(),
        "operation": source.get("operation") or "structured",
        "model": source.get("model"),
        "schema": source.get("schema"),
        "ok": source.get("ok"),
        "ms": source.get("ms"),
    }
    if response_attempts:
        response["attempts"] = response_attempts
    if isinstance(top_stream, dict):
        response["stream"] = top_stream
    for key in ("error", "final_error", "raw", "parsed"):
        if key in source:
            response[key] = source[key]
    return request, response, chunks


def _write_chunks(path: Path, chunks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, mode="wt", encoding="utf-8", compresslevel=6) as stream:
        for chunk in chunks:
            stream.write(json.dumps(chunk, ensure_ascii=False))
            stream.write("\n")


def log_llm_call(record: dict[str, Any]) -> str:
    """Persist one compact call index plus exact request/response/raw artifacts.

    Returns the call id so a caller can put it on the trace events it emits next;
    joining agent turns to calls by line order stopped working when reading went
    concurrent.
    """
    call_id = str(record.get("call_id") or f"llm_{uuid.uuid4().hex}")
    timestamp = _now()
    enriched = {"t": timestamp, **_envelope(call_id=call_id), **record}
    run_dir = _ACTIVE_RUN_DIR
    if run_dir is None:
        logging.getLogger(LLM_LOGGER).debug("llm", extra={"record": enriched})
        return call_id

    with _WRITE_LOCK:
        request, response, chunks = _split_llm_record(enriched, call_id)
        request_path = run_dir / "calls" / f"{call_id}.request.json"
        response_path = run_dir / "calls" / f"{call_id}.response.json"
        _write_json(request_path, request)
        _write_json(response_path, response)
        stream_path = run_dir / "streams" / f"{call_id}.jsonl.gz"
        if chunks:
            _write_chunks(stream_path, chunks)

        elapsed = response.get("ms")
        if elapsed is None and isinstance(response.get("stream"), dict):
            elapsed = response["stream"].get("elapsed_ms")
        index = {
            "t": timestamp,
            **_envelope(call_id=call_id),
            "event": "llm.call",
            "operation": enriched.get("operation") or "structured",
            "model": enriched.get("model"),
            "schema": enriched.get("schema"),
            "call": enriched.get("call"),
            "ok": enriched.get("ok", not bool(enriched.get("error"))),
            "elapsed_ms": elapsed,
            "attempts": len(enriched.get("attempts") or []) or 1,
            "request_ref": _artifact_ref(request_path),
            "response_ref": _artifact_ref(response_path),
            "raw_stream_ref": _artifact_ref(stream_path) if chunks else None,
            "raw_chunks": len(chunks),
        }
        logging.getLogger(LLM_LOGGER).debug("llm", extra={"record": index})
        logging.getLogger(TRACE_LOGGER).debug("llm.call", extra={"record": index})
    return call_id


def _persist_tool_result(tool_call_id: str, name: str, content: str) -> dict[str, Any]:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    run_dir = _ACTIVE_RUN_DIR
    if run_dir is None:
        return {"result_ref": "", "result_sha256": digest, "chars": len(content)}
    identity = f"{current_log_context().get('agent_run_id', '')}:{tool_call_id}"
    artifact_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    path = run_dir / "tools" / f"{artifact_id}.json"
    _write_json(
        path,
        {
            **_envelope(tool_call_id=tool_call_id),
            "t": _now(),
            "tool": name,
            "chars": len(content),
            "sha256": digest,
            "content": content,
        },
    )
    return {
        "result_ref": _artifact_ref(path),
        "result_sha256": digest,
        "chars": len(content),
    }


def log_trace(event: str, **fields: Any) -> None:
    """Append one normalized timeline event with enough IDs to join its artifacts.

    Every call must be reconstructible on its own: pass the ids, the inputs and the
    outcome, not a sentence about them.

    ``t`` is stamped here rather than left to line order, which stopped meaning
    chronological order when reading went concurrent.

    ``document_id`` is filled in from the document being worked on unless the caller
    passes one. Filtering by it is meant to reconstruct a whole run, and the lines that
    did not carry it -- every subdivision decision -- were exactly the ones worth
    reading when a document ends up somewhere surprising.

    A tool result is written out whole and referenced; the timeline keeps a preview.
    A 200-character preview was all there used to be, which is not enough to say what
    the model actually saw.
    """
    payload = dict(fields)
    if event.startswith("agent.tool_") and payload.get("id"):
        payload.setdefault("tool_call_id", payload["id"])
    if event.startswith("agent.tool_") and "content" in payload:
        content = str(payload.pop("content"))
        identity = _persist_tool_result(
            str(payload.get("id") or f"tool_{uuid.uuid4().hex}"),
            str(payload.get("name") or "unknown"),
            content,
        )
        payload.update(identity)
        payload["preview"] = content[:200]
    record = {"t": _now(), "event": event, **_envelope(), **payload}
    logging.getLogger(TRACE_LOGGER).debug(event, extra={"record": record})
