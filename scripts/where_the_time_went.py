"""Summarise one Bismuth run without loading raw provider chunks.

    python scripts/where_the_time_went.py [LOG_DIR_OR_RUN_DIR]

The compact LLM index and timeline are authoritative for timing. Exact prompts,
responses, tool results and compressed chunks are opened only when a debugger asks for
them; this summary never needs to parse the raw stream archive.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _paths(argument: Path) -> tuple[Path, Path, Path]:
    if (argument / "timeline.jsonl").exists():
        return argument / "timeline.jsonl", argument / "llm.jsonl", argument.parents[1]
    return argument / "trace.jsonl", argument / "llm.jsonl", argument


def _elapsed(record: dict[str, Any]) -> int:
    if record.get("elapsed_ms") is not None:
        return int(record["elapsed_ms"])
    if record.get("ms") is not None:
        return int(record["ms"])
    stream = record.get("stream") or {}
    return int(stream.get("elapsed_ms") or 0)


def _kind(record: dict[str, Any]) -> str:
    return str(record.get("schema") or record.get("stage") or record.get("operation") or "unknown")


def _usage(record: dict[str, Any], *, artifact_root: Path) -> tuple[int, int]:
    reference = record.get("response_ref")
    if reference:
        response = json.loads((artifact_root / reference).read_text(encoding="utf-8"))
        stream = response.get("stream") or {}
        usage = stream.get("usage") or {}
        if usage:
            return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        attempts = response.get("attempts") or []
        if attempts:
            last = attempts[-1]
            return int(last.get("in_tokens") or 0), int(last.get("out_tokens") or 0)
    attempts = record.get("attempts") or []
    if attempts:
        last = attempts[-1]
        return int(last.get("in_tokens") or 0), int(last.get("out_tokens") or 0)
    return 0, 0


def main(argv: list[str]) -> int:
    supplied = Path(argv[1]) if len(argv) > 1 else Path("logs")
    trace_path, calls_path, artifact_root = _paths(supplied)
    events = _records(trace_path)
    calls = _records(calls_path)
    if not events and not calls:
        print(f"분석할 로그가 없습니다: {supplied}")
        return 1

    _overview(events, calls)
    _documents(events)
    _maintenance(events)
    _calls(calls, artifact_root=artifact_root)
    return 0


def _overview(events: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
    times = [datetime.fromisoformat(str(item["t"])) for item in [*events, *calls] if item.get("t")]
    wall = (max(times) - min(times)).total_seconds() if times else 0
    run_ids = {str(item.get("run_id")) for item in [*events, *calls] if item.get("run_id")}
    print("실행 개요")
    print(f"  run_id: {', '.join(sorted(run_ids)) or '(legacy log)'}")
    print(f"  관측 구간: {wall / 60:.1f}분")
    print(f"  timeline events: {len(events)}")
    print(f"  LLM calls: {len(calls)}")
    print()


def _documents(events: list[dict[str, Any]]) -> None:
    filed = [event for event in events if event.get("event") == "document.filed"]
    read = {event["document_id"]: event for event in events if event.get("event") == "document.read"}
    if not filed:
        return
    reading = sum(
        int(item.get("parse_ms") or 0) + int(item.get("card_ms") or 0) for item in read.values()
    )
    placement = sum(int(item.get("place_ms") or 0) for item in filed)
    print(f"문서 처리 {len(filed)}건")
    print(f"  읽기+카드: {reading / 60_000:.1f}분")
    print(f"  배치 판단: {placement / 60_000:.1f}분")
    print()


def _maintenance(events: list[dict[str, Any]]) -> None:
    starts = {
        event.get("window_id"): event
        for event in events
        if event.get("event") == "maintenance.window_started"
    }
    finishes = [
        event for event in events if event.get("event") == "maintenance.window_finished"
    ]
    if not starts and not finishes:
        agent_turns = [event for event in events if event.get("event") == "agent.turn"]
        if agent_turns:
            print("구조 유지보수")
            print(f"  agent turns: {len(agent_turns)} (legacy log: window timing unavailable)")
            print()
        return
    print(f"구조 유지보수 {max(len(starts), len(finishes))}개 창")
    total = 0.0
    for finish in finishes:
        start = starts.get(finish.get("window_id"))
        elapsed = 0.0
        if start is not None:
            elapsed = (
                datetime.fromisoformat(str(finish["t"]))
                - datetime.fromisoformat(str(start["t"]))
            ).total_seconds()
            total += elapsed
        print(
            f"  {finish.get('window_id')}: {elapsed:.1f}초, "
            f"moved={finish.get('moved', 0)}, status={finish.get('status', '')}"
        )
    print(f"  합계: {total / 60:.1f}분")
    print()


def _calls(calls: list[dict[str, Any]], *, artifact_root: Path) -> None:
    if not calls:
        return
    by_kind: dict[str, list[int]] = defaultdict(list)
    usage_by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for record in calls:
        kind = _kind(record)
        by_kind[kind].append(_elapsed(record))
        incoming, outgoing = _usage(record, artifact_root=artifact_root)
        usage_by_kind[kind][0] += incoming
        usage_by_kind[kind][1] += outgoing

    print("LLM 호출 — agent 포함")
    print(f"{'종류':<22}{'횟수':>6}{'합계(분)':>10}{'중앙(초)':>10}{'최대(초)':>10}{'입력tok':>12}{'출력tok':>12}")
    for kind, times in sorted(by_kind.items(), key=lambda item: -sum(item[1])):
        incoming, outgoing = usage_by_kind[kind]
        print(
            f"{kind:<22}{len(times):>6}{sum(times) / 60_000:>10.1f}"
            f"{median(times) / 1000:>10.1f}{max(times) / 1000:>10.1f}"
            f"{incoming:>12}{outgoing:>12}"
        )
    print()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
