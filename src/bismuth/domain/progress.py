"""What Bismuth is doing to a document right now, as a value the UI can render.

A document takes a while: parsing, then a model call per window, then placement.
Reporting only "working on it" makes a slow pipeline look like a hung one, so each
step says what it is doing and what it found.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Stage(StrEnum):
    """The steps of ingesting one document, in the order they happen."""

    RECEIVED = "received"
    """Saved to the inbox and journalled. Nothing clever has happened yet."""

    DUPLICATE = "duplicate"
    """Same bytes already ingested; the rest of the pipeline is skipped."""

    PARSING = "parsing"
    PARSED = "parsed"
    READING = "reading"
    """One window of the document is with the model. Repeats."""

    DENSIFYING = "densifying"
    CARDED = "carded"
    PLACING = "placing"
    PLACED = "placed"
    FILING = "filing"
    NOTES = "notes"
    DIVIDING = "dividing"
    """A folder is being examined for a distinction worth drawing."""

    REVIEWING = "reviewing"
    """A folder that was divided before is being asked whether that still holds."""

    DIVIDED = "divided"
    DONE = "done"
    FAILED = "failed"


_TERMINAL = frozenset({Stage.DONE, Stage.FAILED, Stage.DUPLICATE})


class Progress(BaseModel):
    """One step, for one document."""

    model_config = ConfigDict(frozen=True)

    stage: Stage
    filename: str
    document_id: str = ""
    step: int = Field(default=0, ge=0, description="Which window, 1-based. 0 when not applicable.")
    steps: int = Field(default=0, ge=0, description="How many windows in total.")
    found: tuple[str, ...] = Field(
        default=(), description="What this step turned up, in the document's own words."
    )
    note: str = Field(
        default="", description="The one fact this step is about: a parser, a folder."
    )

    @property
    def terminal(self) -> bool:
        """Whether this document is finished, one way or another."""
        return self.stage in _TERMINAL

    @property
    def fraction(self) -> float | None:
        """How far along, or None when this step has no measure.

        Needs both a position and a total: placement knows how many folders it is
        weighing but not which one it is on, and reporting that as 0% would show
        progress running backwards after the reading steps filled the bar.
        """
        if self.step <= 0 or self.steps <= 0:
            return None
        return min(self.step / self.steps, 1.0)

    def label(self) -> str:
        """One Korean line for a person watching. The UI may render its own instead."""
        match self.stage:
            case Stage.RECEIVED:
                return "받았습니다 — 인박스에 먼저 저장"
            case Stage.DUPLICATE:
                return f"같은 내용이 이미 {self.note} 에 있습니다"
            case Stage.PARSING:
                return f"글자 추출 중 ({self.note})"
            case Stage.PARSED:
                return f"{self.note} — 이제 읽습니다"
            case Stage.READING:
                base = f"{self.step}/{self.steps}조각 읽는 중"
                if not self.found:
                    return base
                # A window can turn up a dozen things; the line is a status, not a list.
                shown = ", ".join(self.found[:3])
                rest = len(self.found) - 3
                return f"{base} — {shown}{f' 외 {rest}개' if rest > 0 else ''}"
            case Stage.DENSIFYING:
                return "찾은 것들을 요약에 반영하는 중"
            case Stage.CARDED:
                return f"무엇인지 파악했습니다 — {self.note}"
            case Stage.PLACING:
                # The first document into an empty vault has nothing to compare against.
                if not self.steps:
                    return "첫 문서라 둘 폴더를 새로 정하는 중"
                return f"기존 폴더 {self.steps}개와 비교해 둘 곳을 정하는 중"
            case Stage.PLACED:
                return f"{self.note} 으로 결정"
            case Stage.FILING:
                return "옮기고 사이드카 쓰는 중"
            case Stage.NOTES:
                return "폴더 노트 갱신 중"
            case Stage.DIVIDING:
                return f"{self.note} 를 나눌지 보는 중"
            case Stage.REVIEWING:
                return f"{self.note} 의 기존 구분이 아직 맞는지 보는 중"
            case Stage.DIVIDED:
                return f"{self.note} 로 나눴습니다"
            case Stage.DONE:
                return f"완료 — {self.note}"
            case Stage.FAILED:
                return f"실패 — {self.note}"


ProgressSink = Callable[[Progress], None]
"""Where a service reports to. Must not block or raise -- progress is never the point
of the call, so a broken listener must not take the ingest down with it."""


def report(sink: ProgressSink | None, progress: Progress) -> None:
    """Report a step, if anyone is listening. Swallows listener failures by contract."""
    if sink is None:
        return
    # Broad on purpose: a broken UI must not fail an ingest.
    with contextlib.suppress(Exception):
        sink(progress)
