"""LiteLLM-backed implementation of :class:`~bismuth.ports.llm.LLM`; degrades across native-schema, JSON-mode, and prompt-embedded tiers, feeding validation errors back as repair turns."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from bismuth.domain.errors import ModelRequestError, StructuredOutputError
from bismuth.logging_setup import log_llm_call
from bismuth.ports.llm import CURRENT_DOCUMENT, Prompt, Usage

SchemaT = TypeVar("SchemaT", bound=BaseModel)

logger = logging.getLogger(__name__)
_litellm: Any = None


def _load_litellm() -> Any:
    """Import LiteLLM lazily, not at module scope, so our own ``.env`` load wins over python-dotenv's upward directory scan on import."""
    global _litellm
    if _litellm is None:
        import litellm

        litellm.suppress_debug_info = True
        litellm.drop_params = True
        _litellm = litellm
    return _litellm


def preload() -> None:
    """Do the deferred LiteLLM import now.

    The deferral exists to win a race against python-dotenv, not to postpone the cost:
    importing LiteLLM takes seconds, and paying that inside the first request makes a
    started server look like a hung one. Call once, after the configuration is loaded.
    """
    _load_litellm()


async def close_clients() -> None:
    """Close LiteLLM's shared async transports during application shutdown."""
    if _litellm is None:
        return
    close = getattr(_litellm, "close_litellm_async_clients", None)
    if close is not None:
        await close()


def usage_of(response: Any, model: str) -> Usage:
    """Tokens and price for one completion. Shared with the chat adapter so the agent's
    calls are counted the same way -- a total that silently omits them is a wrong total.

    ``cost_usd`` is None when LiteLLM has no published rate for the model (local models,
    anything unlisted); that is reported rather than guessed at.
    """
    raw = getattr(response, "usage", None)
    try:
        cost = _load_litellm().completion_cost(completion_response=response)
    except Exception:
        cost = None
    return Usage(
        model=model,
        document_id=CURRENT_DOCUMENT.get(""),
        input_tokens=getattr(raw, "prompt_tokens", 0) or 0,
        output_tokens=getattr(raw, "completion_tokens", 0) or 0,
        cost_usd=cost,
    )


_OPENAI_BODY_PARAMS = frozenset(
    {
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "seed",
        "n",
        "logit_bias",
        "user",
    }
)
"""Body values LiteLLM understands as its own arguments. Everything else has to be
smuggled past it -- see :func:`apply_body`."""


def apply_body(kwargs: dict[str, Any], body: dict[str, Any]) -> None:
    """Merge configured request-body values into a completion call, in place.

    Split because LiteLLM runs with ``drop_params``, which silently discards arguments
    the provider is not known to support -- and that is exactly the set worth
    configuring: ``top_k``, ``min_p``, and the ``chat_template_kwargs`` that turns a
    qwen model's thinking off. Those go through ``extra_body``, which is passed to the
    endpoint untouched. The standard ones stay top-level so LiteLLM can translate them
    per provider.

    Configured values win over Bismuth's own, including ``temperature``: structured
    calls default to 0.0 for determinism, and a server that wants otherwise is the one
    that has to be satisfied.
    """
    if not body:
        return
    extra: dict[str, Any] = dict(kwargs.get("extra_body") or {})
    for name, value in body.items():
        if name in _OPENAI_BODY_PARAMS:
            kwargs[name] = value
        else:
            extra[name] = value
    if extra:
        kwargs["extra_body"] = extra


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


_JSON_INSTRUCTION = """\
Reply with a single JSON object and nothing else. No prose, no markdown fences, \
no explanation before or after. It must validate against this JSON Schema:

{schema}
"""

_REPAIR_INSTRUCTION = """\
Your previous reply did not validate.

You sent:
{raw}

The validator said:
{error}

Reply again with the corrected JSON object only. Fix exactly what the validator \
objected to and change nothing else.
"""

_CHOICE_RETRY_SYSTEM = """\
Return exactly one allowed literal and nothing else. Do not use JSON, quotes, prose,
markdown, or an answer wrapper.
"""

# Initial operational caps measured against real successful calls. They are safety
# ceilings, not a substitute for schema validation. Unknown extension schemas get the
# conservative general cap rather than running unbounded.
_SCHEMA_MAX_TOKENS: dict[str, int] = {
    "CharterDraft": 256,
    "BoundaryAudit": 256,
    "ReplacementAudit": 256,
    "RoutingAudit": 256,
    "Review": 256,
    "ExistingAssignments": 256,
    "DensifiedSummary": 512,
    "Members": 512,
    "ReplacementSketch": 512,
    "Replacement": 768,
    "ReplacementAssignments": 1024,
    "CardDraft": 2048,
    "CardUpdate": 2048,
    "Emerging": 4096,
}
_DEFAULT_SCHEMA_MAX_TOKENS = 2048
_REPAIR_RAW_CHARS = 2000


class _RepetitionDetectedError(RuntimeError):
    def __init__(self, pattern: str) -> None:
        super().__init__(f"repeated output pattern {pattern!r}")
        self.pattern = pattern


class _InactivityTimeoutError(TimeoutError):
    pass


class _AbsoluteTimeoutError(TimeoutError):
    pass


class LiteLLMAdapter:
    """Talks to whatever model the configuration points at."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        api_base: str | None = None,
        timeout: float = 120.0,
        absolute_timeout: float = 180.0,
        max_schema_retries: int = 2,
        max_concurrency: int = 4,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        native_schema: bool | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._api_base = api_base
        self._headers = dict(headers or {})
        self._body = dict(body or {})
        self._native_schema = native_schema
        self._timeout = timeout
        self._absolute_timeout = absolute_timeout
        self._max_retries = max_schema_retries
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._usage: list[Usage] = []
        self._native_schema_support: dict[str, bool] = {}
        self._calls = 0

    # -- LLM port -----------------------------------------------------------

    async def structured(
        self,
        prompt: Prompt,
        *,
        schema: type[SchemaT],
        temperature: float = 0.0,
    ) -> SchemaT:
        model = self._model
        native = self._supports_native_schema(model)

        messages = self._build_messages(prompt, schema=schema, native=native)
        last_error = ""
        last_raw = ""

        call_id = self._next_call_id()
        record: dict[str, Any] = {
            "call": call_id,
            "model": model,
            "schema": schema.__name__,
            "native_schema": native,
            "system": prompt.system,
            "user": prompt.user,
            "attempts": [],
        }

        began = time.monotonic()
        for attempt in range(self._max_retries + 1):
            started = time.monotonic()
            attempt_log: dict[str, Any] = {"n": attempt + 1}
            record["attempts"].append(attempt_log)
            try:
                raw, usage = await self._call(
                    model,
                    messages,
                    schema=schema if native else None,
                    temperature=temperature,
                    max_tokens=_schema_output_cap(schema),
                    attempt_log=attempt_log,
                )
            except _RepetitionDetectedError as exc:
                elapsed_ms = round((time.monotonic() - started) * 1000)
                raw = str(attempt_log.get("stream", {}).get("content", ""))
                last_raw = raw
                last_error = str(exc)
                attempt_log.update(ms=elapsed_ms, raw=raw, error=last_error)
                logger.warning(
                    "stopped repeated %s output on attempt %d/%d: %s",
                    schema.__name__,
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
                if attempt == self._max_retries:
                    break
                messages = [
                    *self._build_messages(prompt, schema=schema, native=native),
                    {
                        "role": "user",
                        "content": _REPAIR_INSTRUCTION.format(
                            raw=raw[:_REPAIR_RAW_CHARS], error=last_error[:1500]
                        ),
                    },
                ]
                continue
            except Exception as exc:
                elapsed_ms = round((time.monotonic() - started) * 1000)
                error = f"{type(exc).__name__}: {exc}"
                attempt_log.update(ms=elapsed_ms, transport_error=error)
                record["ok"] = False
                record["ms"] = round((time.monotonic() - began) * 1000)
                record["final_error"] = error
                log_llm_call(record)
                if isinstance(exc, _AbsoluteTimeoutError):
                    raise ModelRequestError(
                        f"LLM 응답 제한시간 {self._absolute_timeout:g}초를 초과했습니다 "
                        "(absolute generation limit)"
                    ) from exc
                if _looks_like_timeout(exc):
                    raise ModelRequestError(
                        f"LLM 응답 제한시간 {self._timeout:g}초를 초과했습니다 "
                        "(stream inactivity limit)"
                    ) from exc
                raise ModelRequestError(f"LLM 요청 실패: {error}") from exc
            # Waiting for the semaphore counts: on a shared endpoint the queue is most
            # of the wall clock, and a duration that excluded it would say every call
            # was fast while the run took an hour.
            elapsed_ms = round((time.monotonic() - started) * 1000)
            self._usage.append(usage.model_copy(update={"retries": attempt}))
            attempt_log.update(
                ms=elapsed_ms,
                raw=raw,
                in_tokens=usage.input_tokens,
                out_tokens=usage.output_tokens,
            )

            try:
                result = schema.model_validate(_parse_json(raw))
                record["ok"] = True
                record["ms"] = round((time.monotonic() - began) * 1000)
                log_llm_call(record)
                return result
            except (ValidationError, ValueError) as exc:
                last_error, last_raw = str(exc), raw
                attempt_log["error"] = str(exc)
                logger.debug(
                    "schema validation failed for %s on attempt %d/%d: %s",
                    model,
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
                if attempt == self._max_retries:
                    break
                # Start a clean request from the original task. The full invalid reply
                # remains in the diagnostic log, but only a bounded fragment is allowed
                # back into model context.
                messages = [
                    *self._build_messages(prompt, schema=schema, native=native),
                    {
                        "role": "user",
                        "content": _REPAIR_INSTRUCTION.format(
                            raw=raw[:_REPAIR_RAW_CHARS], error=last_error[:1500]
                        ),
                    },
                ]

        record["ok"] = False
        record["ms"] = round((time.monotonic() - began) * 1000)
        record["final_error"] = last_error
        log_llm_call(record)
        raise StructuredOutputError(
            f"{model} did not produce valid {schema.__name__} after "
            f"{self._max_retries + 1} attempts.\n"
            f"Last error: {last_error}\n"
            f"Last reply: {last_raw[:500]}\n"
            f"If this model is small, either raise BISMUTH_LLM_MAX_SCHEMA_RETRIES or "
            f"choose a more capable model."
        )

    async def choose(
        self,
        prompt: Prompt,
        *,
        choices: Sequence[str],
        max_tokens: int = 32,
        temperature: float = 0.0,
    ) -> str:
        """Return one exact provider-neutral literal, retrying once without bad context."""
        allowed = tuple(dict.fromkeys(choice.strip().upper() for choice in choices))
        if not allowed or any(not choice for choice in allowed):
            raise ValueError("choices must contain non-empty literals")
        allowed_line = ", ".join(allowed)
        base_messages = [
            {"role": "system", "content": prompt.system},
            {
                "role": "user",
                "content": f"{prompt.user}\n\nALLOWED OUTPUTS: {allowed_line}",
            },
        ]
        clean_messages = [
            {"role": "system", "content": _CHOICE_RETRY_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Choose the correct literal for this filing task.\n\n{prompt.user}"
                    f"\n\nALLOWED OUTPUTS: {allowed_line}"
                ),
            },
        ]
        call_id = self._next_call_id()
        record: dict[str, Any] = {
            "call": call_id,
            "model": self._model,
            "schema": "PlainChoice",
            "native_schema": False,
            "system": prompt.system,
            "user": prompt.user,
            "allowed": list(allowed),
            "attempts": [],
        }
        began = time.monotonic()
        last_raw = ""
        last_error = ""
        for attempt, messages in enumerate((base_messages, clean_messages)):
            started = time.monotonic()
            attempt_log: dict[str, Any] = {"n": attempt + 1, "messages": messages}
            record["attempts"].append(attempt_log)
            try:
                raw, usage = await self._call(
                    self._model,
                    messages,
                    schema=None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    attempt_log=attempt_log,
                    force_temperature=True,
                )
            except _RepetitionDetectedError as exc:
                last_error = str(exc)
                last_raw = str(attempt_log.get("stream", {}).get("content", ""))
                attempt_log.update(
                    ms=round((time.monotonic() - started) * 1000),
                    raw=last_raw,
                    error=last_error,
                )
                if attempt == 0:
                    continue
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                attempt_log.update(
                    ms=round((time.monotonic() - started) * 1000),
                    transport_error=error,
                )
                record.update(
                    ok=False,
                    ms=round((time.monotonic() - began) * 1000),
                    final_error=error,
                )
                log_llm_call(record)
                raise ModelRequestError(f"LLM choice request failed: {error}") from exc

            self._usage.append(usage.model_copy(update={"retries": attempt}))
            attempt_log.update(
                ms=round((time.monotonic() - started) * 1000),
                raw=raw,
                in_tokens=usage.input_tokens,
                out_tokens=usage.output_tokens,
            )
            last_raw = raw
            selected = raw.strip().upper()
            if selected in allowed:
                record.update(ok=True, ms=round((time.monotonic() - began) * 1000))
                log_llm_call(record)
                return selected
            last_error = f"reply {raw[:200]!r} is not one exact allowed literal"
            attempt_log["error"] = last_error

        record.update(
            ok=False,
            ms=round((time.monotonic() - began) * 1000),
            final_error=last_error,
        )
        log_llm_call(record)
        raise StructuredOutputError(
            f"{self._model} did not return one allowed choice after 2 attempts. "
            f"Last error: {last_error}. Last reply: {last_raw[:200]!r}"
        )

    def _next_call_id(self) -> str:
        self._calls += 1
        return f"#{self._calls}"

    def drain_usage(self) -> list[Usage]:
        drained, self._usage = self._usage, []
        return drained

    def _supports_native_schema(self, model: str) -> bool:
        """Whether decoding can be constrained to the schema.

        Configured first: LiteLLM answers from a table of models it knows, so a
        self-hosted endpoint is always "no" there even when it does support it, and the
        difference is a repair turn on every reply the model gets slightly wrong.
        """
        if self._native_schema is not None:
            return self._native_schema
        if model not in self._native_schema_support:
            try:
                self._native_schema_support[model] = bool(
                    _load_litellm().supports_response_schema(model=model)
                )
            except Exception:
                self._native_schema_support[model] = False
        return self._native_schema_support[model]

    def _build_messages(
        self, prompt: Prompt, *, schema: type[BaseModel], native: bool
    ) -> list[dict[str, Any]]:
        system = prompt.system
        if not native:
            # Tier 2/3: embed the schema in the prompt since nothing downstream enforces it.
            system = f"{system}\n\n" + _JSON_INSTRUCTION.format(
                schema=json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)
            )

        system_message: dict[str, Any] = {"role": "system", "content": system}
        if prompt.cache_hint:
            # Anthropic-style prompt caching; drop_params discards this for providers that don't support it.
            system_message["content"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]

        return [system_message, {"role": "user", "content": prompt.user}]

    async def _call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        schema: type[BaseModel] | None,
        temperature: float,
        max_tokens: int,
        attempt_log: dict[str, Any],
        force_temperature: bool = False,
    ) -> tuple[str, Usage]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": self._timeout,
            "stream": True,
            "stream_options": {"include_usage": True},
            # OpenAI-compatible clients otherwise retry a timed-out local request twice.
            # The server may keep generating the abandoned requests, turning one
            # 120-second timeout into a six-minute queue that looks like a dead batch.
            "max_retries": 0,
        }
        if self._api_key:
            # Explicit every call, so a stale key in os.environ can't silently
            # override the one configured in .env.
            kwargs["api_key"] = self._api_key
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if self._headers:
            # Some endpoints sit behind a gateway that authenticates with a cookie or
            # its own header; without these the call never reaches the model.
            kwargs["extra_headers"] = self._headers
        apply_body(kwargs, self._body)
        if force_temperature:
            kwargs["temperature"] = temperature
        # Config may request a smaller budget, but it cannot raise a task's reviewed
        # safety ceiling. Use one OpenAI-compatible spelling to avoid sending two
        # contradictory limits.
        configured_limits = [
            value
            for value in (kwargs.pop("max_tokens", None), kwargs.pop("max_completion_tokens", None))
            if isinstance(value, int) and value > 0
        ]
        kwargs["max_tokens"] = min([max_tokens, *configured_limits])
        if schema is not None:
            kwargs["response_format"] = schema

        stream_log: dict[str, Any] = {
            "chunks": [],
            "content": "",
            "reasoning_content": "",
            "finish_reason": None,
            "completed": False,
        }
        attempt_log["stream"] = stream_log
        attempt_log["messages"] = messages
        chunks: list[Any] = []
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        began = time.monotonic()
        previous = began

        async with self._semaphore:
            response_stream: Any = None
            absolute = asyncio.timeout(self._absolute_timeout)
            try:
                async with absolute:
                    response_stream = await _load_litellm().acompletion(**kwargs)
                    iterator: AsyncIterator[Any] = response_stream.__aiter__()
                    repeat_tail = ""
                    generated_chars = 0
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                iterator.__anext__(), timeout=self._timeout
                            )
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            raise _InactivityTimeoutError(
                                f"no stream chunk received for {self._timeout:g} seconds: {exc}"
                            ) from exc
                        now = time.monotonic()
                        chunks.append(chunk)
                        choice = _first_choice(chunk)
                        delta = _field(choice, "delta")
                        content = _text_field(delta, "content")
                        reasoning = _text_field(delta, "reasoning_content") or _text_field(
                            delta, "reasoning"
                        )
                        finish_reason = _field(choice, "finish_reason")
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
                        if finish_reason is not None:
                            stream_log["finish_reason"] = str(finish_reason)
                        previous = now
                        generated_chars += len(content)
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
            except TimeoutError as exc:
                if absolute.expired():
                    stream_log["abort"] = {
                        "kind": "absolute_timeout",
                        "seconds": self._absolute_timeout,
                    }
                    raise _AbsoluteTimeoutError(
                        f"generation exceeded {self._absolute_timeout:g} seconds"
                    ) from exc
                raise
            finally:
                # Preserve the complete partial output even when reading the next chunk
                # raises a timeout. Joining once avoids quadratic work on long streams.
                stream_log["content"] = "".join(content_parts)
                stream_log["reasoning_content"] = "".join(reasoning_parts)
                if response_stream is not None and not stream_log["completed"]:
                    await _close_stream(response_stream)

        stream_log["completed"] = True
        complete = _load_litellm().stream_chunk_builder(chunks=chunks, messages=messages)
        if complete is None:
            raise RuntimeError("LLM stream ended without a response")
        return stream_log["content"], self._usage_of(complete, model)

    @staticmethod
    def _usage_of(response: Any, model: str) -> Usage:
        return usage_of(response, model)


def _looks_like_timeout(exc: Exception) -> bool:
    names = " ".join(item.__name__ for item in exc.__class__.__mro__)
    return "timeout" in names.casefold() or "timed out" in str(exc).casefold()


def _schema_output_cap(schema: type[BaseModel]) -> int:
    return _SCHEMA_MAX_TOKENS.get(schema.__name__, _DEFAULT_SCHEMA_MAX_TOKENS)


def _repeated_suffix(text: str, *, count: int = 6, maximum_pattern: int = 40) -> str | None:
    """Find only an obvious exact non-whitespace suffix loop."""
    for size in range(2, min(maximum_pattern, len(text) // count) + 1):
        pattern = text[-size:]
        if pattern.strip() and text.endswith(pattern * count):
            return pattern
    return None


async def _close_stream(stream: Any) -> None:
    """Best-effort cancellation; never hide the response or error being logged."""
    try:
        closer = getattr(stream, "aclose", None)
        if closer is not None:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
            return
        closer = getattr(stream, "close", None)
        if closer is not None:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
    except Exception:
        logger.debug("failed to close LLM stream", exc_info=True)


def _field(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _text_field(value: Any, name: str) -> str:
    found = _field(value, name)
    return found if isinstance(found, str) else ""


def _first_choice(chunk: Any) -> Any:
    choices = _field(chunk, "choices") or []
    return choices[0] if choices else None


def _dump_chunk(chunk: Any) -> Any:
    """Preserve every field LiteLLM exposes for the received stream chunk."""
    dump = getattr(chunk, "model_dump", None)
    if dump is not None:
        return dump(mode="json")
    if isinstance(chunk, dict):
        return chunk
    return str(chunk)


def _parse_json(raw: str) -> Any:
    """Recover a JSON object from a model reply: strict parse, then a fenced code block, then a balanced-brace scan.

    Raises:
        ValueError: if no JSON object can be found.
    """
    text = raw.strip()
    if not text:
        raise ValueError("model returned an empty response")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if fenced := _FENCE.search(text):
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    if (obj := _first_balanced_object(text)) is not None:
        return obj

    raise ValueError(f"no JSON object found in response: {text[:200]!r}")


def _first_balanced_object(text: str) -> Any | None:
    """Scan for the first brace-balanced object, respecting string literals (a naive find/rfind slice breaks on nested braces)."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None
