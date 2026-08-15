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


async def run(name: str, documents: list[Path], *, reverse: bool) -> int:
    vault = RUNS / name
    logs = RUNS / f"{name}-logs"
    for path in (vault, logs):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)

    load_env_file()
    configure_logging(log_dir=logs)
    settings = Settings(vault_path=vault)
    update_run_manifest(
        run_name=name,
        vault_path=str(vault),
        documents=len(documents),
        order="reverse" if reverse else "forward",
        model=settings.model,
    )
    engine = build(settings)
    litellm_adapter.preload()
    engine.parsers.warm()

    ordered = list(reversed(documents)) if reverse else documents
    began = time.time()
    failed = 0
    for index, path in enumerate(ordered, start=1):
        rel = engine.ingest.stage(path.read_bytes(), path.name)
        try:
            result = await engine.ingest.process(rel)
            where = str(result.destination) or "/"
        except Exception as exc:  # one document must not end the round
            failed += 1
            where = f"FAILED {type(exc).__name__}: {exc}"
        print(
            f"  [{index:>3}/{len(ordered)}] {time.time() - began:6.0f}s  {where[:48]:<50}{path.name[:44]}"
        )

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
    args = parser.parse_args(argv)

    documents = choose(args.source, args.docs, seed=args.seed)
    print(f"{args.name}: {len(documents)} documents from {args.source}")
    return asyncio.run(run(args.name, documents, reverse=args.reverse))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
