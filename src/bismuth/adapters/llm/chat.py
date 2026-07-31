"""LiteLLM-backed :class:`agentkit.ChatModel`: native tool-calling for the agent loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from agentkit import AssistantMessage, Message, ToolCall, ToolSpec
from bismuth.adapters.llm.litellm_adapter import _load_litellm


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
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._api_base = api_base
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)

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

        async with self._semaphore:
            response = await _load_litellm().acompletion(**kwargs)

        return _from_wire(response.choices[0].message)


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
