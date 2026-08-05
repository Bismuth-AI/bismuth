"""Fan-out of ingest progress to whatever browser tab is watching.

In-memory and unpersisted on purpose: this is a localhost tool with one user, and a
step that nobody saw is not worth keeping. The rule that matters is that reporting
never slows the pipeline down -- a watcher that cannot keep up loses steps, not the
ingest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

from bismuth.domain.progress import Progress

KEEPALIVE_SECONDS = 15.0
"""How long to wait before emitting a comment line. Also how a dropped browser tab is
noticed: the write fails, the generator is cancelled, the subscriber is dropped."""


class ProgressBus:
    """Publishes ingest steps to every open subscriber."""

    def __init__(self, *, backlog: int = 256) -> None:
        self._backlog = backlog
        self._subscribers: set[asyncio.Queue[Progress]] = set()

    def publish(self, event: Progress) -> None:
        """Non-blocking by contract: called from the ingest loop, which must not wait on a UI."""
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
    """One Server-Sent Event. The Korean label ships with it so every client says the same thing."""
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
