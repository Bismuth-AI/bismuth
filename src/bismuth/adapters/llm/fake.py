"""Scripted model for tests and offline use."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

from pydantic import BaseModel

from bismuth.domain.errors import ModelRequestError, StructuredOutputError
from bismuth.ports.llm import CURRENT_DOCUMENT, Prompt, Usage

SchemaT = TypeVar("SchemaT", bound=BaseModel)

Handler = Callable[[Prompt, type[BaseModel] | None], BaseModel | str]


class FakeLLM:
    """Return scripted responses and record calls."""

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

    async def text(
        self,
        prompt: Prompt,
        *,
        max_tokens: int,
        temperature: float = 0.0,
    ) -> str:
        """Whatever the script offers, as a string. ``None`` is the open-text key."""
        del max_tokens, temperature
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
            raise ModelRequestError("FakeLLM ran out of scripted responses (wanted open text)")
        return response if isinstance(response, str) else str(response)

    async def choose(
        self,
        prompt: Prompt,
        *,
        choices: Sequence[str],
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

        if isinstance(response, str):
            selected = response.strip().upper()
        else:
            raise StructuredOutputError(f"FakeLLM choice needs str, got {type(response).__name__}")
        # Allow invalid scripts so service boundaries can be tested.
        return selected

    def drain_usage(self) -> list[Usage]:
        drained, self._usage = self._usage, []
        return drained

    def set_handler(self, handler: Handler) -> None:
        """Replace the response handler for subsequent calls."""
        self._handler = handler

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def prompts_for(self, schema: type[BaseModel] | None) -> list[Prompt]:
        """Return prompts sent for ``schema``."""
        return [p for p, s in self.calls if s is schema]
