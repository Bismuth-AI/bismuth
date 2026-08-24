"""Read model-call diagnostics from run logs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from bismuth.logging_setup import LOG_DIR, active_run_dir

router = APIRouter(prefix="/api/runs", tags=["diagnostics"])

SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")

PREVIEW = 240

INDEX_LIMIT = 32
_INDEX: dict[str, tuple[tuple[tuple[int, int], ...], dict[str, Any]]] = {}


def _stamp(*paths: Path) -> tuple[tuple[int, int], ...]:
    result = []
    for path in paths:
        try:
            stat = path.stat()
            result.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.append((0, 0))
    return tuple(result)


def runs_root() -> Path:
    """Return the directory containing run logs."""
    active = active_run_dir()
    return active.parent if active is not None else (LOG_DIR / "runs").resolve()


def _run_dir(run_id: str) -> Path:
    if not SAFE.match(run_id):
        raise HTTPException(400, "bad run id")
    path = runs_root() / run_id
    if not path.is_dir():
        raise HTTPException(404, f"no run {run_id}")
    return path


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a run being written can end mid-line
    return out


def _read(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _indexed(run_id: str) -> dict[str, Any]:
    """Calls and events for one run, re-read only when its log has grown."""
    run = _run_dir(run_id)
    llm = run / "llm.jsonl"
    timeline = run / "timeline.jsonl"
    manifest = run / "manifest.json"
    stamp = _stamp(llm, timeline, manifest)
    cached = _INDEX.get(run_id)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    calls = [d for d in _lines(llm) if d.get("call_id")]
    documents: dict[str, str] = {}
    events = []
    for event in _lines(timeline):
        if event.get("event") == "llm.call":
            continue  # the same calls, said twice
        if (name := event.get("filename")) and (doc := event.get("document_id")):
            documents.setdefault(str(doc), str(name))
        events.append(event)
    built = {
        "manifest": _read(manifest),
        "calls": calls,
        "events": events,
        "documents": documents,
    }
    _INDEX[run_id] = (stamp, built)
    while len(_INDEX) > INDEX_LIMIT:
        _INDEX.pop(next(iter(_INDEX)))
    return built


def _summary(run_id: str) -> dict[str, Any]:
    built = _indexed(run_id)
    calls, manifest = built["calls"], built["manifest"]
    stages: dict[str, dict[str, Any]] = {}
    for call in calls:
        row = stages.setdefault(str(call.get("stage") or "?"), {"calls": 0, "ms": 0})
        row["calls"] += 1
        row["ms"] += int(call.get("elapsed_ms") or 0)
    return {
        "run_id": run_id,
        "started_at": manifest.get("started_at"),
        "status": manifest.get("status"),
        "model": manifest.get("model"),
        "vault": manifest.get("vault_path"),
        "calls": len(calls),
        "failed": sum(1 for call in calls if call.get("ok") is False),
        "documents": len(built["documents"]),
        "first_at": calls[0]["t"] if calls else None,
        "last_at": calls[-1]["t"] if calls else None,
        "stages": stages,
        "active": (active := active_run_dir()) is not None and active.name == run_id,
    }


@router.get("")
def runs(limit: Annotated[int, Query(ge=1, le=100)] = 25) -> list[dict[str, Any]]:
    """Every run this vault has recorded, newest first."""
    root = runs_root()
    if not root.is_dir():
        return []
    found = sorted(
        (p for p in root.iterdir() if p.is_dir() and SAFE.fullmatch(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    return [_summary(path.name) for path in found[:limit]]


@router.get("/{run_id}/calls")
def calls(run_id: str) -> dict[str, Any]:
    """Return call metadata without prompt and response payloads."""
    built = _indexed(run_id)
    rows = []
    for call in built["calls"]:
        document = str(call.get("document_id") or "")
        rows.append(
            {
                "call_id": call.get("call_id"),
                "t": call.get("t"),
                "stage": call.get("stage"),
                "n": call.get("call"),
                "document_id": document,
                "filename": built["documents"].get(document, ""),
                "window_id": call.get("window_id"),
                "folder": call.get("folder"),
                "operation": call.get("operation"),
                "ok": call.get("ok"),
                "ms": call.get("elapsed_ms"),
                "attempts": call.get("attempts"),
            }
        )
    return {"run": _summary(run_id), "calls": rows, "documents": built["documents"]}


@router.get("/{run_id}/events")
def events(run_id: str, event: str = "", document_id: str = "") -> list[dict[str, Any]]:
    """The pipeline's own decisions, which is where a call's consequence is recorded."""
    rows: list[dict[str, Any]] = _indexed(run_id)["events"]
    if event:
        wanted = {name.strip() for name in event.split(",") if name.strip()}
        rows = [row for row in rows if row.get("event") in wanted]
    if document_id:
        rows = [row for row in rows if str(row.get("document_id") or "") == document_id]
    return rows


@router.get("/{run_id}/search")
def search(
    run_id: str,
    q: str,
    where: str = "all",
    limit: Annotated[int, Query(ge=1, le=1000)] = 300,
) -> dict[str, Any]:
    """Find calls whose prompt or response contains ``q``."""
    run = _run_dir(run_id)
    needle = q.strip().lower()
    if not needle:
        return {"query": q, "hits": [], "scanned": 0, "truncated": False}
    built = _indexed(run_id)
    hits: list[dict[str, Any]] = []
    scanned = 0
    for row in built["calls"]:
        if len(hits) >= limit:
            return {"query": q, "hits": hits, "scanned": scanned, "truncated": True}
        call_id = str(row.get("call_id"))
        found: dict[str, list[str]] = {}
        for side, suffix in (("input", "request"), ("output", "response")):
            if where not in ("all", side):
                continue
            path = run / "calls" / f"{call_id}.{suffix}.json"
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            scanned += 1
            if needle not in raw.lower():
                continue
            body = _read(path)
            fields = (
                [body.get("system") or "", body.get("user") or ""]
                if side == "input"
                else [
                    (attempt.get("stream") or {}).get("content") or ""
                    for attempt in body.get("attempts", [])
                ]
            )
            if snippets := _snippets(fields, needle):
                found[side] = snippets
        if found:
            document = str(row.get("document_id") or "")
            hits.append(
                {
                    "call_id": call_id,
                    "stage": row.get("stage"),
                    "t": row.get("t"),
                    "ms": row.get("elapsed_ms"),
                    "document_id": document,
                    "filename": built["documents"].get(document, ""),
                    "where": sorted(found),
                    "snippets": found,
                }
            )
    return {"query": q, "hits": hits, "scanned": scanned, "truncated": False}


SNIPPET = 90


def _snippets(fields: list[str], needle: str, *, most: int = 3) -> list[str]:
    """The match in its sentence, which is what makes a hit worth clicking."""
    out: list[str] = []
    for text in fields:
        low, at = text.lower(), 0
        while len(out) < most and (found := low.find(needle, at)) != -1:
            start, end = max(0, found - SNIPPET), min(len(text), found + len(needle) + SNIPPET)
            piece = text[start:end].replace("\n", " ⏎ ")
            out.append(("…" if start else "") + piece + ("…" if end < len(text) else ""))
            at = found + len(needle)
    return out


@router.get("/{run_id}/calls/{call_id}")
def call(run_id: str, call_id: str) -> dict[str, Any]:
    """Exactly what was sent and exactly what came back, for one call."""
    if not SAFE.match(call_id):
        raise HTTPException(400, "bad call id")
    run = _run_dir(run_id)
    request, response = (
        _read(run / "calls" / f"{call_id}.request.json"),
        _read(run / "calls" / f"{call_id}.response.json"),
    )
    if not request and not response:
        raise HTTPException(404, f"no call {call_id}")
    index: dict[str, Any] = next(
        (row for row in _indexed(run_id)["calls"] if row.get("call_id") == call_id),
        {},
    )
    tries = []
    for attempt in response.get("attempts", []):
        stream = attempt.get("stream") or {}
        tries.append(
            {
                "n": attempt.get("n"),
                "ms": attempt.get("ms"),
                "max_tokens": attempt.get("max_tokens"),
                "content": stream.get("content") or "",
                "reasoning": stream.get("reasoning_content") or "",
                "finish_reason": stream.get("finish_reason"),
                "completed": stream.get("completed"),
                "error": attempt.get("error") or stream.get("error"),
            }
        )
    documents = _indexed(run_id)["documents"]
    document = str(request.get("document_id") or index.get("document_id") or "")
    return {
        "call_id": call_id,
        "stage": request.get("stage") or index.get("stage"),
        "t": request.get("t") or index.get("t"),
        "model": request.get("model") or index.get("model"),
        "operation": request.get("operation"),
        "document_id": document,
        "filename": documents.get(document, ""),
        "window_id": request.get("window_id"),
        "folder": request.get("folder"),
        "schema": request.get("schema"),
        "native_schema": request.get("native_schema"),
        "system": request.get("system") or "",
        "user": request.get("user") or "",
        "ok": response.get("ok", index.get("ok")),
        "ms": response.get("ms", index.get("ms")),
        "attempts": tries,
    }
