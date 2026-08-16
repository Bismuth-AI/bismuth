"""One tuning round: a fresh vault, N documents, then the numbers. Reads saved settings.

    python scripts/ralph.py <name> --docs 24 [--source DIR] [--reverse] [--seed N]

The vault goes under ``~/bismuth-runs/<name>`` and the logs under
``~/bismuth-runs/<name>-logs`` -- outside the vault, because a log file inside it is a
sibling folder as far as the model is concerned.

Documents are spread evenly across the sorted corpus rather than taken from the front,
which would draw them all from whichever family sorts first. ``--reverse`` feeds the same
selection in the opposite order, which is how order-independence gets measured.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import shutil
import sys
import time
from pathlib import Path

from bismuth.adapters.llm import litellm_adapter
from bismuth.config import Settings, load_env_file
from bismuth.container import build
from bismuth.logging_setup import configure_logging, finish_run_manifest, update_run_manifest
from bismuth.services.ingest import Prepared

DEFAULT_SOURCE = Path.home() / "Documents" / "카카오톡 받은 파일" / "법제처_300" / "flat"
RUNS = Path.home() / "bismuth-runs"


def choose(source: Path, count: int, *, seed: int | None) -> list[Path]:
    every = sorted(source.glob("*.pdf")) or sorted(p for p in source.iterdir() if p.is_file())
    if count >= len(every):
        return every
    if seed is not None:
        return sorted(random.Random(seed).sample(every, count))
    step = len(every) / count
    return [every[int(index * step)] for index in range(count)]


async def run(
    name: str, documents: list[Path], *, reverse: bool, ahead: int, concurrency: int
) -> int:
    vault = RUNS / name
    logs = RUNS / f"{name}-logs"
    for path in (vault, logs):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)

    load_env_file()
    configure_logging(log_dir=logs)
    # The benchmark pushes the gateway harder than the server does. Raised here rather
    # than in the saved config, so a tuning round cannot slow down anyone else's day.
    settings = Settings(vault_path=vault, llm_max_concurrency=concurrency)
    update_run_manifest(
        run_name=name,
        vault_path=str(vault),
        documents=len(documents),
        order="reverse" if reverse else "forward",
        read_ahead=ahead,
        concurrency=concurrency,
        model=settings.model,
    )
    engine = build(settings)
    litellm_adapter.preload()
    engine.parsers.warm()

    ordered = list(reversed(documents)) if reverse else documents
    began = time.time()
    failed = 0

    # Reading and carding is 57% of a round's model time and depends on nothing but the
    # document itself -- ingest.prepare reads no folder and writes nothing. So it runs
    # ahead, several documents at a time, while filing stays strictly in order behind it.
    # Filing is where order carries meaning: the tree a document is placed into is the
    # tree the documents before it built. Nothing about what gets decided changes.
    queue: asyncio.Queue[tuple[int, Path, Prepared | Exception]] = asyncio.Queue(maxsize=ahead)

    async def read_ahead() -> None:
        """Card several documents at once, handing them over in the original order."""
        done: dict[int, tuple[Path, Prepared | Exception]] = {}
        next_out = 1
        gate = asyncio.Semaphore(ahead)
        lock = asyncio.Lock()

        async def one(index: int, path: Path) -> None:
            nonlocal next_out
            async with gate:
                rel = engine.ingest.stage(path.read_bytes(), path.name)
                try:
                    prepared: Prepared | Exception = await engine.ingest.prepare(rel)
                except Exception as exc:  # reported per document below, like filing does
                    prepared = exc
            async with lock:
                done[index] = (path, prepared)
                while next_out in done:
                    await queue.put((next_out, *done.pop(next_out)))
                    next_out += 1

        await asyncio.gather(*(one(i, p) for i, p in enumerate(ordered, start=1)))

    reader = asyncio.create_task(read_ahead())
    for _ in ordered:
        index, path, prepared = await queue.get()
        if isinstance(prepared, Exception):
            failed += 1
            where = f"FAILED {type(prepared).__name__}: {prepared}"
        else:
            try:
                result = await engine.ingest.file(prepared)
                where = str(result.destination) or "/"
            except Exception as exc:  # one document must not end the round
                failed += 1
                where = f"FAILED {type(exc).__name__}: {exc}"
        print(
            f"  [{index:>3}/{len(ordered)}] {time.time() - began:6.0f}s"
            f"  {where[:48]:<50}{path.name[:44]}"
        )
    await reader

    finish_run_manifest()
    await litellm_adapter.close_clients()
    print(
        f"\n{name}: {len(ordered) - failed}/{len(ordered)} in {(time.time() - began) / 60:.1f} min"
    )
    print(f"  vault {vault}\n  logs  {logs}")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--docs", type=int, default=24)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--ahead",
        type=int,
        default=8,
        help="how many documents are read and carded ahead of filing (1 = the old serial run)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=12,
        help="model calls in flight at once; 20 dropped documents on one run, 12 did not",
    )
    args = parser.parse_args(argv)

    documents = choose(args.source, args.docs, seed=args.seed)
    print(f"{args.name}: {len(documents)} documents from {args.source}")
    return asyncio.run(
        run(
            args.name,
            documents,
            reverse=args.reverse,
            ahead=args.ahead,
            concurrency=args.concurrency,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
