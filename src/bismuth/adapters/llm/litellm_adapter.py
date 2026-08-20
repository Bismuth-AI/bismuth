"""LiteLLM-backed implementation of :class:`~bismuth.ports.llm.LLM`; degrades across native-schema, JSON-mode, and prompt-embedded tiers, feeding validation errors back as repair turns."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from bismuth.adapters.llm.body import (
    _drop_unsupported,
    _schema_output_cap,
    apply_body,
)
from bismuth.adapters.llm.wire import (
    _AbsoluteTimeoutError,
    _close_stream,
    _dump_chunk,
    _field,
    _first_choice,
    _InactivityTimeoutError,
    _load_litellm,
    _looks_like_repetition,
    _looks_like_timeout,
    _open_stream,
    _parse_json,
    _repeated_suffix,
    _RepetitionDetectedError,
    _shared_aiohttp_session,
    _text_field,
    close_clients,
    preload,
)
from bismuth.domain.errors import ModelRequestError, StructuredOutputError
from bismuth.logging_setup import log_llm_call
from bismuth.ports.llm import CURRENT_DOCUMENT, Prompt, Usage

SchemaT = TypeVar("SchemaT", bound=BaseModel)

logger = logging.getLogger(__name__)

# Re-exported so callers keep one import for the provider adapter: the lifecycle lives in
# :mod:`wire` because it belongs to the transport, not to this class.
__all__ = ["LiteLLMAdapter", "close_clients", "preload", "usage_of"]


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


"""Body values LiteLLM understands as its own arguments. Everything else has to be
smuggled past it -- see :func:`apply_body`."""
"""How the model picks its next token. A classification call owns these; see
:func:`apply_body`."""


_MINIMAL_REASONING = "minimal"
"""What a classification call asks a reasoning model to spend on itself.

A reasoning model's ``max_tokens`` is a total, not an output cap: its thinking is billed
against the same budget and never streamed. Measured on gpt-5-nano at default effort --
a placement choice returned ``out_tokens: 32``, ``finish_reason: length`` and an empty
reply, twice, so every document failed; a card spent 2048 tokens and then 4096 the same
way. There is nothing to reason about in these calls anyway. They ask a closed question
with the answers listed, which is the same thing qwen's ``enable_thinking: false``
already turns off, and thinking there cost 93 seconds a document instead of 6.

LiteLLM removes the parameter for models it knows have no reasoning to configure, and an
endpoint that refuses it by name is honoured through :func:`_learn_refusal`. An operator
who wants more can set ``reasoning_effort`` in the configured body.
"""


def _loggable_parameters(
    kwargs: dict[str, Any], *, schema: type[BaseModel] | None
) -> dict[str, Any]:
    """The generation settings this attempt actually ran with, minus the credentials.

    An allowlist, not a blocklist: ``kwargs`` carries the api key and gateway headers,
    and a diagnostic file is the last place either should be able to reach. Recorded
    per attempt because a retry can change the cap and drop native schema enforcement,
    and because a repetition cannot be blamed on or cleared of sampling without knowing
    what the sampling was.
    """
    logged: dict[str, Any] = {
        name: kwargs[name]
        for name in (
            "model",
            "temperature",
            "max_tokens",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "reasoning_effort",
            "stream",
        )
        if name in kwargs
    }
    if extra := kwargs.get("extra_body"):
        logged["extra_body"] = extra
    logged["native_schema"] = schema is not None
    return logged


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

_LENGTH_INSTRUCTION = """\
Your previous reply was cut off because it ran past the generation limit.

Answer the same task again, but shorter. Keep every field, and make the free-text ones \
brief: a few sentences, not a recitation of the source. Do not enumerate items the \
schema does not ask for. A complete short answer is required; a long one is discarded.
"""

_CHOICE_RETRY_SYSTEM = """\
Return exactly one allowed literal and nothing else. Do not use JSON, quotes, prose,
markdown, or an answer wrapper.
"""
_MAX_SCHEMA_MAX_TOKENS = 8192
_CHOICE_MAX_TOKENS = 64
"""What one literal out of a listed set is allowed to cost.

A ceiling is free when the model stops on its own: an answer of ``F003`` is three tokens
whether the ceiling is 8 or 64. The only thing a small one can do is cut the answer off,
and for a model whose thinking is invisible it cuts before the answer starts. Measured on
gpt-5-nano at minimal effort: 8 tokens spent with nothing shown, then ``SHELF`` in 12.
"""
_MAX_CHOICE_MAX_TOKENS = 512
_REPAIR_RAW_CHARS = 2000
_STALLED_WHITESPACE_CHARS = 512


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
        output_cap = _schema_output_cap(schema)

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
                    max_tokens=output_cap,
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
                # A native constrained decoder can itself be the source of a loop: JSON
                # whitespace remains legal forever when it cannot choose the next field.
                # Retry from the original task, without poisoned partial output, using
                # prompt-embedded JSON instead of the same decoder path.
                native = False
                messages = self._build_messages(prompt, schema=schema, native=False)
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

            if attempt_log["stream"].get("finish_reason") == "length":
                last_raw = raw
                effective_cap = int(attempt_log.get("max_tokens", output_cap))
                # "output reached the limit" was read off finish_reason alone, and it said
                # that about a reasoning model that had produced nothing at all: the cap is
                # a total, and the thinking billed against it is never streamed. Doubling
                # the budget cannot fix that, so the message must not imply it can.
                last_error = (
                    f"the {effective_cap}-token budget was spent without producing any "
                    "output; a reasoning model counts its own thinking against it"
                    if not raw.strip()
                    else f"output reached the {effective_cap}-token generation limit"
                )
                attempt_log["error"] = last_error
                logger.warning(
                    "%s %s on attempt %d/%d",
                    schema.__name__,
                    (
                        f"produced nothing within {effective_cap} tokens"
                        if not raw.strip()
                        else f"output reached {effective_cap} tokens"
                    ),
                    attempt + 1,
                    self._max_retries + 1,
                )
                if effective_cap < output_cap:
                    # A user-configured API body deliberately imposed the lower cap;
                    # retrying cannot raise it and would reproduce the same truncation.
                    break
                next_cap = min(max(output_cap, effective_cap) * 2, _MAX_SCHEMA_MAX_TOKENS)
                if attempt == self._max_retries or next_cap <= effective_cap:
                    break
                output_cap = next_cap
                # Say what went wrong. Silently doubling the budget is fuel when the
                # cause is a model enumerating rather than a document that is genuinely
                # long: one card filled 2048, then 4096, then 8192 tokens and the
                # document was lost. A reply that stops is worth more than a longer one.
                messages = [
                    *self._build_messages(prompt, schema=schema, native=native),
                    {"role": "user", "content": _LENGTH_INSTRUCTION},
                ]
                continue

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
            "Inspect finish_reason and the preserved stream before changing retry counts."
        )

    async def text(
        self,
        prompt: Prompt,
        *,
        max_tokens: int,
        temperature: float = 0.0,
    ) -> str:
        """Open text, with a budget and no grammar.

        The same transport, the same repetition breaker, the same logging as every other
        call -- the only difference is that nothing constrains the tokens. Measured over
        twelve documents, a JSON schema compiled to a grammar emptied a nested array in
        21 of 36 replies and cut eleven labels mid-word at its own ceiling, while a line
        format asked for in the prompt lost neither.

        One clean retry if the reply is empty. There is no schema to repair against, so a
        second attempt starts from the original task rather than from the bad answer.
        """
        model = self._model
        call_id = self._next_call_id()
        record: dict[str, Any] = {
            "call": call_id,
            "model": model,
            "schema": None,
            "native_schema": False,
            "system": prompt.system,
            "user": prompt.user,
            "attempts": [],
        }
        began = time.monotonic()
        attempts: list[dict[str, Any]] = record["attempts"]
        last_error = ""

        for attempt in range(2):
            attempt_log: dict[str, Any] = {"n": attempt + 1}
            attempts.append(attempt_log)
            started = time.monotonic()
            try:
                raw, usage = await self._call(
                    model,
                    [
                        {"role": "system", "content": prompt.system},
                        {"role": "user", "content": prompt.user},
                    ],
                    schema=None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    attempt_log=attempt_log,
                )
            except _RepetitionDetectedError as exc:
                raw = str(attempt_log.get("stream", {}).get("content", ""))
                last_error = str(exc)
                attempt_log.update(ms=round((time.monotonic() - started) * 1000), error=last_error)
                # Whatever arrived before the loop is still the model's answer to this
                # task, and a line format loses only the lines after the break.
                if raw.strip():
                    record["ok"] = True
                    record["ms"] = round((time.monotonic() - began) * 1000)
                    log_llm_call(record)
                    return raw
                continue
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                attempt_log.update(
                    ms=round((time.monotonic() - started) * 1000), transport_error=last_error
                )
                if attempt:
                    break
                continue
            self._usage.append(usage.model_copy(update={"retries": attempt}))
            attempt_log["ms"] = round((time.monotonic() - started) * 1000)
            if raw.strip():
                record["ok"] = True
                record["ms"] = round((time.monotonic() - began) * 1000)
                log_llm_call(record)
                return raw
            last_error = "the model returned nothing"

        record["ok"] = False
        record["ms"] = round((time.monotonic() - began) * 1000)
        record["final_error"] = last_error
        log_llm_call(record)
        raise ModelRequestError(f"{model} returned no text: {last_error}")

    async def choose(
        self,
        prompt: Prompt,
        *,
        choices: Sequence[str],
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
        cap = _CHOICE_MAX_TOKENS
        # A queue rather than a fixed pair, because the two recoveries are different
        # questions. The stripped prompt is for a model that answered with the wrong kind
        # of thing; a spent budget means the real question was never answered at all, and
        # asking a smaller version of it instead is a worse question, not a retry.
        pending = [base_messages, clean_messages]
        attempt = -1
        while pending:
            attempt += 1
            messages = pending.pop(0)
            started = time.monotonic()
            attempt_log: dict[str, Any] = {"n": attempt + 1, "messages": messages}
            record["attempts"].append(attempt_log)
            try:
                raw, usage = await self._call(
                    self._model,
                    messages,
                    schema=None,
                    temperature=temperature,
                    max_tokens=cap,
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
                continue
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
            if not selected and attempt_log.get("stream", {}).get("finish_reason") == "length":
                # Nothing was output and the budget is gone, so it went somewhere invisible:
                # a reasoning model bills its thinking against the same cap. Raising it is
                # safe here in a way it is not for a card -- a closed choice's output is
                # bounded by the literals, so the extra room can only absorb thinking.
                last_error = (
                    f"the {cap}-token budget was spent without producing any output; "
                    "a reasoning model counts its own thinking against it"
                )
                attempt_log["error"] = last_error
                logger.warning("%s produced no choice within %d tokens", self._model, cap)
                if (raised := min(cap * 8, _MAX_CHOICE_MAX_TOKENS)) > cap:
                    cap = raised
                    pending.insert(0, messages)  # the same question, with room to answer it
                continue
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
        # Every call through this adapter is schema-bound or a closed choice, so Bismuth
        # owns how the token is picked. The agent chat path keeps the operator's values.
        apply_body(kwargs, self._body, owns_sampling=True)
        if "reasoning_effort" not in kwargs:
            kwargs["reasoning_effort"] = _MINIMAL_REASONING
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
        attempt_log["max_tokens"] = kwargs["max_tokens"]
        if schema is not None:
            kwargs["response_format"] = schema
        _drop_unsupported(kwargs)
        attempt_log["request_parameters"] = _loggable_parameters(kwargs, schema=schema)

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
                    if shared_session := await _shared_aiohttp_session():
                        kwargs["shared_session"] = shared_session
                    given_up: list[str] = []
                    response_stream = await _open_stream(kwargs, given_up=given_up)
                    if given_up:
                        attempt_log["parameters_refused"] = given_up
                        attempt_log["request_parameters"] = _loggable_parameters(
                            kwargs, schema=schema
                        )
                    iterator: AsyncIterator[Any] = response_stream.__aiter__()
                    repeat_tail = ""
                    generated_chars = 0
                    whitespace_tail_chars = 0
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
            except Exception as exc:
                if _looks_like_repetition(exc):
                    stream_log["abort"] = {
                        "kind": "repetition",
                        "pattern": "<provider repeated stream chunk>",
                        "after_chars": generated_chars,
                    }
                    raise _RepetitionDetectedError("<provider repeated stream chunk>") from exc
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
