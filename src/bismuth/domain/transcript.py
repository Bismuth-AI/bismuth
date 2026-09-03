"""What was asked and answered, written down so a conversation can be reopened later.

A transcript keeps the exchange a person can read — question, answer, which tools ran —
and not the wire messages. Reopening rebuilds the model's history from these pairs, which
also drops the tool traffic the next turn does not need.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

TITLE_CHARS = 60
"""How much of the opening question stands in for the whole conversation."""


def _now() -> datetime:
    return datetime.now(UTC)


class TranscriptTurn(BaseModel):
    """One exchange, kept so the next question can lean on it."""

    model_config = ConfigDict(frozen=True)

    question: str
    answer: str
    tools: list[str] = Field(default_factory=list)
    asked_at: datetime = Field(default_factory=_now)


class Transcript(BaseModel):
    """Everything said in one conversation."""

    id: str
    turns: list[TranscriptTurn] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def title(self) -> str:
        """The opening question, shortened. Conversations are found by what began them."""
        return _shorten(self.turns[0].question if self.turns else "")

    def summary(self) -> TranscriptSummary:
        return TranscriptSummary(
            id=self.id,
            title=self.title,
            turns=len(self.turns),
            started_at=self.started_at,
            updated_at=self.updated_at,
        )


class TranscriptSummary(BaseModel):
    """One line in a history list: enough to choose a conversation without loading it."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    turns: int
    started_at: datetime
    updated_at: datetime


def _shorten(question: str) -> str:
    line = " ".join(question.split())
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1] + "…"
