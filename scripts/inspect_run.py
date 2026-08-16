"""Inspect one run's compact timeline and selected exact artifacts.

Read the small timeline first and open exact evidence only when a specific step is in
question. Reading hundreds of thousands of raw chunks to answer a classification
question is slow and mostly noise.

Examples:
    python scripts/inspect_run.py logs
    python scripts/inspect_run.py logs --document <document_id>
    python scripts/inspect_run.py logs --stage subdivision.review
    python scripts/inspect_run.py logs --event subdivide.rejected
    python scripts/inspect_run.py logs --call llm_<id>
    python scripts/inspect_run.py logs --artifact runs/<run>/tools/<id>.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def _run_dir(path: Path) -> tuple[Path, Path]:
    """Accept either a run directory or the logs root; return (run dir, artifact root)."""
    if (path / "manifest.json").exists():
        return path, path.parents[1]
    latest = json.loads((path / "latest.json").read_text(encoding="utf-8"))
    return path / latest["path"], path


def _records(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="logs")
    parser.add_argument("--document", help="document_id: the join key for one document's whole run")
    parser.add_argument(
        "--window", help="window_id: one card window or one bounded evidence packet"
    )
    parser.add_argument("--stage")
    parser.add_argument("--event")
    parser.add_argument("--call", help="call_id: print the exact request and response")
    parser.add_argument("--artifact", help="path from a result_ref or request_ref")
    parser.add_argument(
        "--folder",
        help="vault path of a folder: every call and trace event that touched it, in order",
    )
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)

    supplied = Path(args.path).resolve()
    run_dir, artifact_root = _run_dir(supplied)
    if args.artifact:
        _print(json.loads((artifact_root / args.artifact).read_text(encoding="utf-8")))
        return 0
    if args.call:
        request = run_dir / "calls" / f"{args.call}.request.json"
        response = run_dir / "calls" / f"{args.call}.response.json"
        _print(
            {
                "request": json.loads(request.read_text(encoding="utf-8")),
                "response": json.loads(response.read_text(encoding="utf-8")),
                "raw_stream": f"streams/{args.call}.jsonl.gz",
            }
        )
        return 0

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    events = _records(run_dir / "timeline.jsonl")
    filters = {
        "document_id": args.document,
        "window_id": args.window,
        "stage": args.stage,
        "event": args.event,
        "folder": args.folder,
    }
    selected = [
        event
        for event in events
        if all(value is None or event.get(key) == value for key, value in filters.items())
    ]
    if any(value is not None for value in filters.values()):
        for event in selected[: args.limit]:
            # Lead with the call id: this listing exists so a defect seen in the finished
            # tree can be followed to the exact request and response that produced it,
            # which is `--call <id>` and step 2 of SPEC.md 6.3.
            if call := event.get("call_id"):
                print(f"# --call {call}")
            print(json.dumps(event, ensure_ascii=False))
        if len(selected) > args.limit:
            print(f"... {len(selected) - args.limit} more event(s)")
        return 0

    _print(
        {
            "manifest": manifest,
            "events": len(events),
            "event_counts": dict(Counter(str(event.get("event")) for event in events)),
            "documents": len(
                {str(event["document_id"]) for event in events if event.get("document_id")}
            ),
            "windows": sorted(
                {str(event["window_id"]) for event in events if event.get("window_id")}
            ),
            "stages": dict(Counter(str(event["stage"]) for event in events if event.get("stage"))),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
