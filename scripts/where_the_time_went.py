"""Where a run's wall clock went, per document and per call. Reads logs; changes nothing.

    python scripts/where_the_time_went.py [LOG_DIR]

Answers the two questions a slow run raises: which documents were slow, and what the
slow ones were doing. A document that spent four minutes on one question put to the root
after it was already filed looks, in any other view, exactly like a document that was
slow to read.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def main(argv: list[str]) -> int:
    logs = Path(argv[1]) if len(argv) > 1 else Path("logs")
    trace, calls = logs / "trace.jsonl", logs / "llm.jsonl"
    if not trace.exists():
        print(f"트레이스가 없습니다: {trace}")
        return 1

    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line]
    filed = [e for e in events if e["event"] == "document.filed"]
    read = {e["document_id"]: e for e in events if e["event"] == "document.read"}

    if not filed:
        print("아직 document.filed 기록이 없습니다 — 이 판은 시간 계측 전에 돌았습니다.")
    else:
        _documents(filed, read)

    if calls.exists():
        _calls(
            [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines() if line]
        )
    return 0


def _documents(filed: list[dict], read: dict[str, dict]) -> None:
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


def _calls(records: list[dict]) -> None:
    timed = [r for r in records if r.get("ms") is not None]
    if not timed:
        print("모델 호출에 시간 기록이 없습니다 — 이 판은 계측 전에 돌았습니다.")
        return

    by_schema: dict[str, list[int]] = defaultdict(list)
    for record in timed:
        by_schema[record["schema"]].append(int(record["ms"]))

    print(f"모델 호출 {len(timed)}회 — 종류별")
    print(f"{'schema':<20}{'횟수':>5}{'합계(분)':>9}{'중앙(초)':>9}{'최대(초)':>9}")
    for schema, times in sorted(by_schema.items(), key=lambda kv: -sum(kv[1])):
        ordered = sorted(times)
        median = ordered[len(ordered) // 2]
        print(
            f"{schema:<20}{len(times):>5}{sum(times) / 1000 / 60:>9.1f}"
            f"{median / 1000:>9.1f}{max(times) / 1000:>9.1f}"
        )

    print()
    print("가장 오래 걸린 호출 10개")
    for record in sorted(timed, key=lambda r: -int(r["ms"]))[:10]:
        last = record["attempts"][-1]
        print(
            f"  {int(record['ms']) / 1000:>7.1f}초  {record['schema']:<18}"
            f" in={last.get('in_tokens'):>6} out={last.get('out_tokens'):>5}"
            f" 시도{len(record['attempts'])}"
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
