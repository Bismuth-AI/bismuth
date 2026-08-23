"""Broadcast ingest progress to browser subscribers."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

from bismuth.domain.progress import Progress

KEEPALIVE_SECONDS = 15.0


class ProgressBus:
    """Publishes ingest steps to every open subscriber."""

    def __init__(self, *, backlog: int = 256) -> None:
        self._backlog = backlog
        self._subscribers: set[asyncio.Queue[Progress]] = set()

    def publish(self, event: Progress) -> None:
        """Publish without blocking ingest work."""
        for queue in list(self._subscribers):
            # A stalled tab drops steps. The alternative is stalling the ingest.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[Progress]]:
        queue: asyncio.Queue[Progress] = asyncio.Queue(maxsize=self._backlog)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    @property
    def watchers(self) -> int:
        return len(self._subscribers)


def as_event(progress: Progress) -> str:
    """Serialize one progress event for SSE."""
    payload = progress.model_dump(mode="json") | {
        "label": progress.label(),
        "terminal": progress.terminal,
        "fraction": progress.fraction,
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream(bus: ProgressBus) -> AsyncIterator[str]:
    """The SSE body: every step, plus a comment line often enough to notice a closed tab."""
    with bus.subscribe() as queue:
        yield ": open\n\n"
        while True:
            try:
                progress = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield as_event(progress)
