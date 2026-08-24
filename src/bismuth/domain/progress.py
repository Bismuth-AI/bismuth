"""Document processing progress values."""

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
        """Return measurable progress, or ``None`` when no position is available."""
        if self.step <= 0 or self.steps <= 0:
            return None
        return min(self.step / self.steps, 1.0)


ProgressSink = Callable[[Progress], None]


def report(sink: ProgressSink | None, progress: Progress) -> None:
    """Report a step, if anyone is listening. Swallows listener failures by contract."""
    if sink is None:
        return
    # Progress listeners must not interrupt document processing.
    with contextlib.suppress(Exception):
        sink(progress)
