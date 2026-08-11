"""A scripted model, for tests and offline demos; shipped rather than kept in ``tests/`` so embedders can test their own integration."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

from pydantic import BaseModel

from bismuth.domain.errors import StructuredOutputError
from bismuth.ports.llm import CURRENT_DOCUMENT, Prompt, Usage

SchemaT = TypeVar("SchemaT", bound=BaseModel)

Handler = Callable[[Prompt, type[BaseModel] | None], BaseModel | str]


class FakeLLM:
    """Returns canned objects and records what it was asked; a ``queue`` returns responses in order, a ``handler`` computes one from the prompt."""

    def __init__(
        self,
        queue: Sequence[BaseModel | str] | None = None,
        *,
        handler: Handler | None = None,
    ) -> None:
        if queue is None and handler is None:
            raise ValueError("FakeLLM needs either a queue or a handler")
        self._queue = list(queue or [])
        self._handler = handler
        self.calls: list[tuple[Prompt, type[BaseModel] | None]] = []
        self._usage: list[Usage] = []

    async def structured(
        self,
        prompt: Prompt,
        *,
        schema: type[SchemaT],
        temperature: float = 0.0,
    ) -> SchemaT:
        self.calls.append((prompt, schema))
        self._usage.append(
            Usage(
                model="fake/model",
                document_id=CURRENT_DOCUMENT.get(""),
                input_tokens=0,
                output_tokens=0,
            )
        )

        if self._handler is not None:
            response = self._handler(prompt, schema)
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

    async def choose(
        self,
        prompt: Prompt,
        *,
        choices: Sequence[str],
        max_tokens: int = 32,
        temperature: float = 0.0,
    ) -> str:
        """Drive a plain-choice script. ``None`` is the handler's choice-task key."""
        self.calls.append((prompt, None))
        self._usage.append(
            Usage(
                model="fake/model",
                document_id=CURRENT_DOCUMENT.get(""),
                input_tokens=0,
                output_tokens=0,
            )
        )
        if self._handler is not None:
            response = self._handler(prompt, None)
        elif self._queue:
            response = self._queue.pop(0)
        else:
            raise StructuredOutputError("FakeLLM ran out of scripted choice responses")

        from bismuth.prompts.placement import PlacementDecision

        if isinstance(response, PlacementDecision):
            raw = response.folder_id
            selected = "UNREADABLE" if raw is None else (raw or "STAY")
        elif isinstance(response, str):
            selected = response.strip().upper()
        else:
            raise StructuredOutputError(f"FakeLLM choice needs str, got {type(response).__name__}")
        # Deliberately let a scripted invalid literal through. Production adapters
        # enforce the allow-list; service tests still need to exercise their own final
        # trust boundary against a faulty adapter.
        return selected

    def drain_usage(self) -> list[Usage]:
        drained, self._usage = self._usage, []
        return drained

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def prompts_for(self, schema: type[BaseModel] | None) -> list[Prompt]:
        """Every prompt sent while asking for ``schema``, so a test can assert on what the service told the model."""
        from bismuth.prompts.placement import PlacementDecision

        if schema is PlacementDecision:
            schema = None
        return [p for p, s in self.calls if s is schema]
