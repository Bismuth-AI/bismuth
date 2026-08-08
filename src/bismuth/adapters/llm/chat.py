"""LiteLLM-backed :class:`agentkit.ChatModel`: native tool-calling for the agent loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from agentkit import AssistantMessage, Message, ToolCall, ToolSpec

from bismuth.adapters.llm.litellm_adapter import _load_litellm, apply_body, usage_of
from bismuth.ports.llm import Usage


class LiteLLMChatModel:
    """Drives one model with tool-calling, adapting agentkit's neutral types to LiteLLM."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        api_base: str | None = None,
        timeout: float = 120.0,
        max_concurrency: int = 4,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._api_base = api_base
        self._headers = dict(headers or {})
        self._body = dict(body or {})
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._usage: list[Usage] = []

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> AssistantMessage:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        wire.extend(_to_wire(m) for m in messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": wire,
            "timeout": self._timeout,
            "temperature": 0.0,
        }
        if tools:
            kwargs["tools"] = [_tool_to_wire(t) for t in tools]
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if self._headers:
            kwargs["extra_headers"] = self._headers
        apply_body(kwargs, self._body)

        async with self._semaphore:
            response = await _load_litellm().acompletion(**kwargs)

        # The agent runs on every upload; leaving its calls out of the total would make
        # the reported cost quietly wrong rather than merely incomplete.
        self._usage.append(usage_of(response, self._model))
        return _from_wire(response.choices[0].message)

    def drain_usage(self) -> list[Usage]:
        """Return usage recorded since the last drain, and reset."""
        drained, self._usage = self._usage, []
        return drained


def _to_wire(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in message.tool_calls
            ],
        }
    return {"role": message.role, "content": message.content}


def _tool_to_wire(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def _from_wire(message: Any) -> AssistantMessage:
    calls = []
    for raw in getattr(message, "tool_calls", None) or []:
        try:
            arguments = json.loads(raw.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
        calls.append(ToolCall(id=raw.id, name=raw.function.name, arguments=arguments))
    return AssistantMessage(text=message.content or "", tool_calls=tuple(calls))
