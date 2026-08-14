"""LiteLLM-backed :class:`agentkit.ChatModel`: native tool-calling for the agent loop."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from agentkit import AssistantMessage, Message, ToolCall, ToolSpec
from agentkit.context import estimate_tokens

from bismuth.adapters.llm.litellm_adapter import (
    _STALLED_WHITESPACE_CHARS,
    _close_stream,
    _dump_chunk,
    _field,
    _first_choice,
    _load_litellm,
    _looks_like_repetition,
    _repeated_suffix,
    _repeated_word_sequence,
    _RepetitionDetectedError,
    _shared_aiohttp_session,
    _text_field,
    apply_body,
    usage_of,
)
from bismuth.logging_setup import log_llm_call
from bismuth.ports.llm import Usage

_DEFAULT_AGENT_MAX_TOKENS = 32_000
_ESCALATED_AGENT_MAX_TOKENS = 64_000
_DEFAULT_CONTEXT_WINDOW_TOKENS = 65_536
_DEFAULT_CONTEXT_SAFETY_TOKENS = 1_024
_MAX_OUTPUT_RECOVERIES = 3
_OUTPUT_RECOVERY_MESSAGE = (
    "Output token limit hit. Resume directly — no apology and no recap. "
    "Pick up where the response stopped, break remaining work into smaller pieces, "
    "and complete the required tool call."
)


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
        max_tokens: int = _DEFAULT_AGENT_MAX_TOKENS,
        context_window_tokens: int = _DEFAULT_CONTEXT_WINDOW_TOKENS,
        context_safety_tokens: int = _DEFAULT_CONTEXT_SAFETY_TOKENS,
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
        self._context_window_tokens = context_window_tokens
        self._context_safety_tokens = context_safety_tokens
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._usage: list[Usage] = []

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        tool_choice: str | None = None,
        _max_tokens_override: int | None = None,
        _recovery_count: int = 0,
        _prefix_text: str = "",
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
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
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
        requested_output_tokens = (
            _max_tokens_override
            if _max_tokens_override is not None
            else min([self._max_tokens, *configured_limits])
        )
        # Output budgets are reservations, not constants.  The endpoint rejects the
        # whole call when input + requested output exceeds its context window, so a
        # nominal 32K/64K policy must always yield to the space actually left by this
        # exact system prompt, transcript, and tool schema.
        estimated_input_tokens = estimate_tokens(
            json.dumps(
                {"messages": wire, "tools": kwargs.get("tools", [])},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        available_output_tokens = (
            self._context_window_tokens
            - estimated_input_tokens
            - self._context_safety_tokens
        )
        if available_output_tokens < 1:
            raise RuntimeError(
                "agent input leaves no output slot: "
                f"estimated_input={estimated_input_tokens}, "
                f"context_window={self._context_window_tokens}, "
                f"safety={self._context_safety_tokens}"
            )
        kwargs["max_tokens"] = min(requested_output_tokens, available_output_tokens)

        call_id = f"llm_{uuid.uuid4().hex}"
        parameters = {
            key: value
            for key, value in kwargs.items()
            if key
            not in {
                "api_key",
                "api_base",
                "extra_headers",
                "messages",
                "tools",
                "shared_session",
            }
        }

        record: dict[str, Any] = {
            "call_id": call_id,
            "operation": "agent_chat",
            "model": self._model,
            "messages": wire,
            "tools": [_tool_to_wire(t) for t in tools],
            "max_tokens": kwargs["max_tokens"],
            "parameters": parameters,
            "output_recovery": {
                "attempt": _recovery_count,
                "max_tokens_override": _max_tokens_override,
            },
            "context_reservation": {
                "context_window_tokens": self._context_window_tokens,
                "estimated_input_tokens": estimated_input_tokens,
                "safety_tokens": self._context_safety_tokens,
                "requested_output_tokens": requested_output_tokens,
                "reserved_output_tokens": kwargs["max_tokens"],
            },
            "stream": {"chunks": [], "completed": False},
        }
        stream_log = record["stream"]
        chunks: list[Any] = []
        response_stream: Any = None
        began = time.monotonic()
        previous = began
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        generated_chars = 0
        whitespace_tail_chars = 0
        repeat_tail = ""
        next_long_repeat_check = 1_024
        guarded_stop = False

        async with self._semaphore:
            absolute = asyncio.timeout(self._absolute_timeout)
            try:
                async with absolute:
                    if shared_session := await _shared_aiohttp_session():
                        kwargs["shared_session"] = shared_session
                    response_stream = await _load_litellm().acompletion(**kwargs)
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
                        choice = _first_choice(chunk)
                        delta = _field(choice, "delta")
                        content = _text_field(delta, "content")
                        reasoning = _text_field(delta, "reasoning_content") or _text_field(
                            delta, "reasoning"
                        )
                        content_parts.append(content)
                        reasoning_parts.append(reasoning)
                        stream_log["chunks"].append(
                            {
                                "n": len(chunks),
                                "ms": round((now - began) * 1000),
                                "gap_ms": round((now - previous) * 1000),
                                "raw": _dump_chunk(chunk),
                            }
                        )
                        previous = now
                        generated_chars += len(content)
                        if content:
                            if content.strip():
                                whitespace_tail_chars = len(content) - len(content.rstrip())
                            else:
                                whitespace_tail_chars += len(content)
                        if whitespace_tail_chars >= _STALLED_WHITESPACE_CHARS:
                            stream_log["abort"] = {
                                "kind": "repetition",
                                "pattern": "<whitespace>",
                                "after_chars": generated_chars,
                            }
                            raise _RepetitionDetectedError("<whitespace>")
                        repeat_tail = (repeat_tail + content)[-240:]
                        if generated_chars >= 24:
                            pattern = _repeated_suffix(repeat_tail)
                            if pattern is not None:
                                stream_log["abort"] = {
                                    "kind": "repetition",
                                    "pattern": pattern,
                                    "after_chars": generated_chars,
                                }
                                raise _RepetitionDetectedError(pattern)
                        if generated_chars >= next_long_repeat_check:
                            next_long_repeat_check = generated_chars + 256
                            pattern = _repeated_word_sequence("".join(content_parts))
                            if pattern is not None:
                                stream_log["abort"] = {
                                    "kind": "repetition",
                                    "pattern": pattern,
                                    "after_chars": generated_chars,
                                    "mode": "repeated_word_sequence",
                                }
                                raise _RepetitionDetectedError(pattern)
            except Exception as exc:
                if _looks_like_repetition(exc) and not isinstance(exc, _RepetitionDetectedError):
                    stream_log["abort"] = {
                        "kind": "repetition",
                        "pattern": "<provider repeated stream chunk>",
                        "after_chars": generated_chars,
                    }
                    exc = _RepetitionDetectedError("<provider repeated stream chunk>")
                if isinstance(exc, _RepetitionDetectedError):
                    # A guarded partial prose reply is safe to return: Agent Kit will
                    # reject prose-only completion and request exactly one conclusion
                    # tool. Raising here would fail the whole maintenance window.
                    guarded_stop = True
                    stream_log["guarded_stop"] = True
                else:
                    stream_log["error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "absolute_timeout": absolute.expired(),
                    }
                    stream_log["elapsed_ms"] = round((time.monotonic() - began) * 1000)
                    stream_log["content"] = "".join(content_parts)
                    stream_log["reasoning_content"] = "".join(reasoning_parts)
                    log_llm_call(record)
                    raise exc
            finally:
                if response_stream is not None and not stream_log["completed"]:
                    await _close_stream(response_stream)

        try:
            response = _load_litellm().stream_chunk_builder(chunks=chunks, messages=wire)
        except Exception as exc:
            stream_log["elapsed_ms"] = round((time.monotonic() - began) * 1000)
            stream_log["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "phase": "stream_chunk_builder",
            }
            log_llm_call(record)
            raise
        if response is None:
            stream_log["elapsed_ms"] = round((time.monotonic() - began) * 1000)
            stream_log["error"] = {"type": "RuntimeError", "message": "empty stream"}
            log_llm_call(record)
            raise RuntimeError("agent LLM stream ended without a response")
        stream_log["completed"] = True
        stream_log["elapsed_ms"] = round((time.monotonic() - began) * 1000)
        choice = response.choices[0]
        message = choice.message
        stream_log["content"] = message.content or ""
        stream_log["reasoning_content"] = getattr(message, "reasoning_content", None) or ""
        stream_log["finish_reason"] = (
            "repetition_guard" if guarded_stop else getattr(choice, "finish_reason", None)
        )
        stream_log["tool_calls"] = [
            {
                "id": raw.id,
                "name": raw.function.name,
                "arguments": raw.function.arguments or "{}",
            }
            for raw in getattr(message, "tool_calls", None) or []
        ]
        usage = usage_of(response, self._model)
        stream_log["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }
        log_llm_call(record)

        # The agent runs on every upload; leaving its calls out of the total would make
        # the reported cost quietly wrong rather than merely incomplete.
        self._usage.append(usage)
        parsed = _from_wire(message)
        finish_reason = stream_log["finish_reason"]
        if finish_reason == "length" and not parsed.tool_calls:
            # Match Claude Code's output-token recovery policy: the economical default
            # reserves 32K, a capped response is retried cleanly at 64K, and a response
            # that still reaches the model limit may continue in up to three bounded
            # follow-up turns. An explicit configured lower cap remains authoritative.
            if (
                _max_tokens_override is None
                and not configured_limits
                and kwargs["max_tokens"] < _ESCALATED_AGENT_MAX_TOKENS
            ):
                return await self.complete(
                    system=system,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    _max_tokens_override=_ESCALATED_AGENT_MAX_TOKENS,
                    _recovery_count=_recovery_count,
                    _prefix_text=_prefix_text,
                )
            if not configured_limits and _recovery_count < _MAX_OUTPUT_RECOVERIES:
                continued_messages = [
                    *messages,
                    Message("assistant", parsed.text),
                    Message("user", _OUTPUT_RECOVERY_MESSAGE),
                ]
                return await self.complete(
                    system=system,
                    messages=continued_messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    _max_tokens_override=_ESCALATED_AGENT_MAX_TOKENS,
                    _recovery_count=_recovery_count + 1,
                    _prefix_text=_prefix_text + parsed.text,
                )
        return AssistantMessage(_prefix_text + parsed.text, parsed.tool_calls, call_id)

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
