"""Submit a deterministic corpus sample through the browser's batch endpoint.

This is a development/evaluation helper, not an alternate ingest path.  It posts the
same multipart ``files`` field to ``/api/batches`` that the drag-and-drop UI uses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

import httpx


def _sample(source: Path, count: int, seed: str) -> list[Path]:
    files = [path for path in source.iterdir() if path.is_file()]
    return sorted(
        files,
        key=lambda path: hashlib.sha256(f"{seed}|{path.name}".encode()).digest(),
    )[:count]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--count", type=int, default=150)
    parser.add_argument("--seed", default="bismuth-ralf-v1")
    parser.add_argument("--url", default="http://127.0.0.1:8765/api/batches")
    args = parser.parse_args()

    chosen = _sample(args.source, args.count, args.seed)
    if len(chosen) != args.count:
        raise SystemExit(f"requested {args.count} files but found {len(chosen)}")

    with ExitStack() as stack:
        files = [
            (
                "files",
                (path.name, stack.enter_context(path.open("rb")), "application/pdf"),
            )
            for path in chosen
        ]
        response = httpx.post(args.url, files=files, timeout=300.0)
        response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False))


if __name__ == "__main__":
    main()
