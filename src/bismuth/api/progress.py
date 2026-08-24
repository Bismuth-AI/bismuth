"""Broadcast ingest progress to browser subscribers."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

from bismuth.domain.progress import Progress, Stage

KEEPALIVE_SECONDS = 15.0


def label(progress: Progress) -> str:
    """Render a progress value for the current user interface."""
    match progress.stage:
        case Stage.RECEIVED:
            return "받았습니다 — 인박스에 먼저 저장"
        case Stage.DUPLICATE:
            return f"같은 내용이 이미 {progress.note} 에 있습니다"
        case Stage.PARSING:
            return f"글자 추출 중 ({progress.note})"
        case Stage.PARSED:
            return f"{progress.note} — 이제 읽습니다"
        case Stage.READING:
            base = f"{progress.step}/{progress.steps}조각 읽는 중"
            if not progress.found:
                return base
            shown = ", ".join(progress.found[:3])
            rest = len(progress.found) - 3
            return f"{base} — {shown}{f' 외 {rest}개' if rest > 0 else ''}"
        case Stage.DENSIFYING:
            return "찾은 것들을 요약에 반영하는 중"
        case Stage.CARDED:
            return f"무엇인지 파악했습니다 — {progress.note}"
        case Stage.PLACING:
            if not progress.steps:
                return "첫 문서라 둘 폴더를 새로 정하는 중"
            return f"기존 폴더 {progress.steps}개와 비교해 둘 곳을 정하는 중"
        case Stage.PLACED:
            return f"{progress.note} 으로 결정"
        case Stage.FILING:
            return "옮기고 사이드카 쓰는 중"
        case Stage.NOTES:
            return "폴더 노트 갱신 중"
        case Stage.DIVIDING:
            return f"{progress.note} 를 나눌지 보는 중"
        case Stage.REVIEWING:
            return f"{progress.note} 의 기존 구분이 아직 맞는지 보는 중"
        case Stage.DIVIDED:
            return f"{progress.note} 로 나눴습니다"
        case Stage.DONE:
            return f"완료 — {progress.note}"
        case Stage.FAILED:
            return f"실패 — {progress.note}"


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
        "label": label(progress),
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
