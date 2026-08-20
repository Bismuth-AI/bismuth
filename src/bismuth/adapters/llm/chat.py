"""LiteLLM-backed :class:`agentkit.ChatModel`: native tool-calling for the agent loop."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from agentkit import AssistantMessage, Message, ToolCall, ToolSpec

from bismuth.adapters.llm.body import _drop_unsupported, apply_body
from bismuth.adapters.llm.litellm_adapter import usage_of
from bismuth.adapters.llm.wire import (
    _close_stream,
    _dump_chunk,
    _load_litellm,
    _open_stream,
    _shared_aiohttp_session,
)
from bismuth.logging_setup import log_llm_call
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
        absolute_timeout: float = 180.0,
        max_tokens: int = 16_384,
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
        self._absolute_timeout = absolute_timeout
        self._max_tokens = max_tokens
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
            "max_retries": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
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
        configured_limits = [
            value
            for value in (kwargs.pop("max_tokens", None), kwargs.pop("max_completion_tokens", None))
            if isinstance(value, int) and value > 0
        ]
        kwargs["max_tokens"] = min([self._max_tokens, *configured_limits])
        _drop_unsupported(kwargs)

        record: dict[str, Any] = {
            "operation": "agent_chat",
            "model": self._model,
            "messages": wire,
            "tools": [_tool_to_wire(t) for t in tools],
            "max_tokens": kwargs["max_tokens"],
            "stream": {"chunks": [], "completed": False},
        }
        stream_log = record["stream"]
        chunks: list[Any] = []
        response_stream: Any = None
        began = time.monotonic()
        previous = began

        async with self._semaphore:
            absolute = asyncio.timeout(self._absolute_timeout)
            try:
                async with absolute:
                    if shared_session := await _shared_aiohttp_session():
                        kwargs["shared_session"] = shared_session
                    response_stream = await _open_stream(kwargs)
                    iterator: AsyncIterator[Any] = response_stream.__aiter__()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                iterator.__anext__(), timeout=self._timeout
                            )
                        except StopAsyncIteration:
                            break
                        now = time.monotonic()
                        chunks.append(chunk)
                        stream_log["chunks"].append(
                            {
                                "n": len(chunks),
                                "ms": round((now - began) * 1000),
                                "gap_ms": round((now - previous) * 1000),
                                "raw": _dump_chunk(chunk),
                            }
                        )
                        previous = now
            except Exception as exc:
                stream_log["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "absolute_timeout": absolute.expired(),
                }
                log_llm_call(record)
                raise
            finally:
                if response_stream is not None and not stream_log["completed"]:
                    await _close_stream(response_stream)

        response = _load_litellm().stream_chunk_builder(chunks=chunks, messages=wire)
        if response is None:
            stream_log["error"] = {"type": "RuntimeError", "message": "empty stream"}
            log_llm_call(record)
            raise RuntimeError("agent LLM stream ended without a response")
        stream_log["completed"] = True
        stream_log["elapsed_ms"] = round((time.monotonic() - began) * 1000)
        log_llm_call(record)

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
