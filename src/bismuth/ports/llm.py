"""The model boundary: services request a profile, not a model, and every call is structured."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class ModelProfile(StrEnum):
    """What a call is worth, expressed as intent rather than as a model name."""

    FAST = "fast"
    """Runs once per document: reading it and cataloguing what it is. Cheap and
    frequent; the budget lives or dies here."""

    REASONING = "reasoning"
    """The decisions worth paying for: placing a document into the tree, and
    drafting a folder's note. Fewer calls than FAST, higher stakes each."""


class Prompt(BaseModel):
    """A single-turn instruction. Bismuth has no use for conversation."""

    model_config = ConfigDict(frozen=True)

    system: str
    user: str
    cache_hint: bool = Field(
        default=False,
        description=(
            "Marks a large, stable prefix (the folder tree and its notes) as "
            "worth caching. Placement re-sends the same tree context for every "
            "document in a batch; providers that support prompt caching make that "
            "nearly free, and those that do not ignore this."
        ),
    )


class Usage(BaseModel):
    """What a call cost. Aggregated for the run report, not for billing."""

    model_config = ConfigDict(frozen=True)

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    retries: int = Field(
        default=0,
        description=(
            "Schema-repair attempts. Persistently non-zero for a profile means the "
            "model behind it is too small for the task -- a diagnostic worth "
            "surfacing rather than swallowing."
        ),
    )


class Spend(BaseModel):
    """What a piece of work cost, summed from the calls it took."""

    model_config = ConfigDict(frozen=True)

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    cost_usd: float | None = Field(
        default=None,
        description="Sum of the calls that carried a price. None when none of them did.",
    )
    priced_calls: int = Field(
        default=0,
        description=(
            "How many of `calls` LiteLLM could price. Fewer than `calls` means the total "
            "is a floor, not a figure -- local and unlisted models have no published rate."
        ),
    )

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def fully_priced(self) -> bool:
        return self.calls > 0 and self.priced_calls == self.calls

    @classmethod
    def of(cls, usages: Iterable[Usage]) -> Spend:
        items = list(usages)
        priced = [u.cost_usd for u in items if u.cost_usd is not None]
        return cls(
            calls=len(items),
            input_tokens=sum(u.input_tokens for u in items),
            output_tokens=sum(u.output_tokens for u in items),
            retries=sum(u.retries for u in items),
            cost_usd=sum(priced) if priced else None,
            priced_calls=len(priced),
        )

    def __add__(self, other: Spend) -> Spend:
        costs = [c for c in (self.cost_usd, other.cost_usd) if c is not None]
        return Spend(
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            retries=self.retries + other.retries,
            cost_usd=sum(costs) if costs else None,
            priced_calls=self.priced_calls + other.priced_calls,
        )


@runtime_checkable
class LLM(Protocol):
    """A model that returns validated objects."""

    async def structured(
        self,
        prompt: Prompt,
        *,
        schema: type[SchemaT],
        profile: ModelProfile = ModelProfile.FAST,
        temperature: float = 0.0,
    ) -> SchemaT:
        """Return a validated instance of ``schema``, retrying with the validation error on failure.

        Raises:
            StructuredOutputError: if no attempt produced a valid instance.
        """
        ...

    def drain_usage(self) -> list[Usage]:
        """Return usage recorded since the last drain, and reset."""
        ...
