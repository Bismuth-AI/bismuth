"""A scripted model, for tests and offline demos; shipped rather than kept in ``tests/`` so embedders can test their own integration."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

from pydantic import BaseModel

from bismuth.domain.errors import StructuredOutputError
from bismuth.ports.llm import ModelProfile, Prompt, Usage

SchemaT = TypeVar("SchemaT", bound=BaseModel)

Handler = Callable[[Prompt, type[BaseModel], ModelProfile], BaseModel]


class FakeLLM:
    """Returns canned objects and records what it was asked; a ``queue`` returns responses in order, a ``handler`` computes one from the prompt."""

    def __init__(
        self,
        queue: Sequence[BaseModel] | None = None,
        *,
        handler: Handler | None = None,
    ) -> None:
        if queue is None and handler is None:
            raise ValueError("FakeLLM needs either a queue or a handler")
        self._queue = list(queue or [])
        self._handler = handler
        self.calls: list[tuple[Prompt, type[BaseModel], ModelProfile]] = []
        self._usage: list[Usage] = []

    async def structured(
        self,
        prompt: Prompt,
        *,
        schema: type[SchemaT],
        profile: ModelProfile = ModelProfile.FAST,
        temperature: float = 0.0,
    ) -> SchemaT:
        self.calls.append((prompt, schema, profile))
        self._usage.append(Usage(model=f"fake/{profile.value}", input_tokens=0, output_tokens=0))

        if self._handler is not None:
            response = self._handler(prompt, schema, profile)
        elif self._queue:
            response = self._queue.pop(0)
        else:
            raise StructuredOutputError(
                f"FakeLLM ran out of scripted responses (wanted {schema.__name__}). "
                f"The service made more calls than the test expected -- usually the "
                f"finding, not the bug."
            )

        if not isinstance(response, schema):
            raise StructuredOutputError(
                f"FakeLLM was scripted with {type(response).__name__} but the service "
                f"asked for {schema.__name__}"
            )
        return response

    def drain_usage(self) -> list[Usage]:
        drained, self._usage = self._usage, []
        return drained

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def prompts_for(self, schema: type[BaseModel]) -> list[Prompt]:
        """Every prompt sent while asking for ``schema``, so a test can assert on what the service told the model."""
        return [p for p, s, _ in self.calls if s is schema]
