"""Where a run's wall clock went, per document and per call. Reads logs; changes nothing.

    python scripts/where_the_time_went.py [LOG_DIR_OR_RUN_DIR]

Answers the two questions a slow run raises: which documents were slow, and what the
slow ones were doing. A document that spent four minutes on one question put to the root
after it was already filed looks, in any other view, exactly like a document that was
slow to read.

The compact index and timeline are authoritative for timing. Token counts come from the
response artifacts; the raw chunk archive is never opened here.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _paths(supplied: Path) -> tuple[Path, Path, Path]:
    """Accept a run directory or the logs root. Returns (timeline, call index, artifact root)."""
    if (supplied / "timeline.jsonl").exists():
        return supplied / "timeline.jsonl", supplied / "llm.jsonl", supplied.parents[1]
    return supplied / "trace.jsonl", supplied / "llm.jsonl", supplied


def main(argv: list[str]) -> int:
    supplied = Path(argv[1]) if len(argv) > 1 else Path("logs")
    trace, calls, artifact_root = _paths(supplied)
    if not trace.exists():
        print(f"트레이스가 없습니다: {trace}")
        return 1

    events = _records(trace)
    filed = [e for e in events if e.get("event") == "document.filed"]
    read = {e["document_id"]: e for e in events if e.get("event") == "document.read"}

    if not filed:
        print("아직 document.filed 기록이 없습니다 — 이 판은 시간 계측 전에 돌았습니다.")
    else:
        _documents(filed, read)

    _calls(_records(calls), artifact_root=artifact_root)
    return 0


def _documents(filed: list[dict[str, Any]], read: dict[str, dict[str, Any]]) -> None:
    print(f"문서 {len(filed)}건 — 오래 걸린 순\n")
    print(f"{'전체':>7}{'읽기':>7}{'카드':>7}{'배치':>7}{'저장':>7}{'노트':>7}{'세분화':>8}  파일")
    rows = sorted(filed, key=lambda e: -e.get("total_ms", 0))
    for event in rows[:20]:
        r = read.get(event["document_id"], {})
        parts = (
            r.get("parse_ms", 0),
            r.get("card_ms", 0),
            event.get("place_ms", 0),
            event.get("commit_ms", 0),
            event.get("notes_ms", 0),
            event.get("subdivide_ms", 0),
        )
        total = event.get("total_ms", 0) + r.get("parse_ms", 0) + r.get("card_ms", 0)
        line = "".join(f"{value / 1000:>7.1f}" for value in parts)
        print(f"{total / 1000:>7.1f}{line[7:]}  {str(event.get('filename', ''))[:46]}")

    total = sum(e.get("total_ms", 0) for e in filed)
    reading = sum(r.get("parse_ms", 0) + r.get("card_ms", 0) for r in read.values())
    stages: dict[str, int] = defaultdict(int)
    for event in filed:
        for key, value in event.items():
            if key.endswith("_ms") and key != "total_ms":
                stages[key] += int(value)
    print()
    print(f"합계 {(total + reading) / 1000 / 60:.1f}분 — 단계별:")
    print(f"  읽기+카드   {reading / 1000 / 60:>6.1f}분")
    for name, value in sorted(stages.items(), key=lambda kv: -kv[1]):
        print(f"  {name[:-3]:<10} {value / 1000 / 60:>6.1f}분")
    print()


def _elapsed(record: dict[str, Any]) -> int:
    for key in ("elapsed_ms", "ms"):
        if record.get(key) is not None:
            return int(record[key])
    stream = record.get("stream") or {}
    return int(stream.get("elapsed_ms") or 0)


def _kind(record: dict[str, Any]) -> str:
    """Schema name for structured calls; the operation for agent chat, which has none."""
    return str(record.get("schema") or record.get("operation") or "unknown")


def _usage(record: dict[str, Any], *, artifact_root: Path) -> tuple[int, int]:
    """Token counts, from the response artifact when the index only references it."""
    sources: list[dict[str, Any]] = [record]
    reference = record.get("response_ref")
    if reference:
        path = artifact_root / reference
        if path.exists():
            sources.insert(0, json.loads(path.read_text(encoding="utf-8")))
    for source in sources:
        usage = (source.get("stream") or {}).get("usage") or {}
        if usage:
            return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        attempts = source.get("attempts")
        if isinstance(attempts, list) and attempts:
            last = attempts[-1]
            return int(last.get("in_tokens") or 0), int(last.get("out_tokens") or 0)
    return 0, 0


def _calls(records: list[dict[str, Any]], *, artifact_root: Path) -> None:
    timed = [r for r in records if _elapsed(r)]
    if not timed:
        print("모델 호출에 시간 기록이 없습니다 — 이 판은 계측 전에 돌았습니다.")
        return

    by_kind: dict[str, list[int]] = defaultdict(list)
    tokens: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for record in timed:
        kind = _kind(record)
        by_kind[kind].append(_elapsed(record))
        incoming, outgoing = _usage(record, artifact_root=artifact_root)
        tokens[kind][0] += incoming
        tokens[kind][1] += outgoing

    # Agent calls are included: leaving them out does not make the total incomplete,
    # it makes it wrong.
    print(f"모델 호출 {len(timed)}회 — 종류별 (agent 포함)")
    print(
        f"{'종류':<20}{'횟수':>5}{'합계(분)':>9}{'중앙(초)':>9}{'최대(초)':>9}{'입력tok':>11}{'출력tok':>11}"
    )
    for kind, times in sorted(by_kind.items(), key=lambda kv: -sum(kv[1])):
        ordered = sorted(times)
        median = ordered[len(ordered) // 2]
        incoming, outgoing = tokens[kind]
        print(
            f"{kind:<20}{len(times):>5}{sum(times) / 1000 / 60:>9.1f}"
            f"{median / 1000:>9.1f}{max(times) / 1000:>9.1f}{incoming:>11}{outgoing:>11}"
        )

    print()
    print("가장 오래 걸린 호출 10개")
    for record in sorted(timed, key=_elapsed, reverse=True)[:10]:
        incoming, outgoing = _usage(record, artifact_root=artifact_root)
        attempts = record.get("attempts")
        count = len(attempts) if isinstance(attempts, list) else int(attempts or 1)
        reference = record.get("call_id") or ""
        print(
            f"  {_elapsed(record) / 1000:>7.1f}초  {_kind(record):<18}"
            f" in={incoming:>6} out={outgoing:>5} 시도{count}  {reference}"
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
