"""LiteLLM-backed implementation of :class:`~bismuth.ports.llm.LLM`; degrades across native-schema, JSON-mode, and prompt-embedded tiers, feeding validation errors back as repair turns."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from bismuth.domain.errors import StructuredOutputError
from bismuth.logging_setup import log_llm_call
from bismuth.ports.llm import CURRENT_DOCUMENT, ModelProfile, Prompt, Usage

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


class LiteLLMAdapter:
    """Talks to whatever model the configuration points at."""

    def __init__(
        self,
        *,
        model_fast: str,
        model_reasoning: str,
        api_key: str = "",
        api_base: str | None = None,
        timeout: float = 120.0,
        max_schema_retries: int = 2,
        max_concurrency: int = 4,
        reasoning_effort: str = "",
    ) -> None:
        self._models = {
            ModelProfile.FAST: model_fast,
            ModelProfile.REASONING: model_reasoning,
        }
        self._api_key = api_key
        self._api_base = api_base
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout
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
        profile: ModelProfile = ModelProfile.FAST,
        temperature: float = 0.0,
    ) -> SchemaT:
        model = self._models[profile]
        native = self._supports_native_schema(model)

        messages = self._build_messages(prompt, schema=schema, native=native)
        last_error = ""
        last_raw = ""

        call_id = self._next_call_id()
        record: dict[str, Any] = {
            "call": call_id,
            "model": model,
            "profile": profile.value,
            "schema": schema.__name__,
            "native_schema": native,
            "system": prompt.system,
            "user": prompt.user,
            "attempts": [],
        }

        for attempt in range(self._max_retries + 1):
            raw, usage = await self._call(
                model,
                messages,
                schema=schema if native else None,
                temperature=temperature,
                profile=profile,
            )
            self._usage.append(usage.model_copy(update={"retries": attempt}))
            attempt_log: dict[str, Any] = {
                "n": attempt + 1,
                "raw": raw,
                "in_tokens": usage.input_tokens,
                "out_tokens": usage.output_tokens,
            }
            record["attempts"].append(attempt_log)

            try:
                result = schema.model_validate(_parse_json(raw))
                record["ok"] = True
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
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": _REPAIR_INSTRUCTION.format(
                            raw=raw[:2000], error=last_error[:1500]
                        ),
                    },
                ]

        record["ok"] = False
        record["final_error"] = last_error
        log_llm_call(record)
        raise StructuredOutputError(
            f"{model} did not produce valid {schema.__name__} after "
            f"{self._max_retries + 1} attempts.\n"
            f"Last error: {last_error}\n"
            f"Last reply: {last_raw[:500]}\n"
            f"If this model is small, either raise BISMUTH_LLM_MAX_SCHEMA_RETRIES or "
            f"point the profile at a larger model."
        )

    def _next_call_id(self) -> str:
        self._calls += 1
        return f"#{self._calls}"

    def drain_usage(self) -> list[Usage]:
        drained, self._usage = self._usage, []
        return drained

    def _supports_native_schema(self, model: str) -> bool:
        """Ask LiteLLM whether the provider constrains decoding to a schema; cached, and unknown models are assumed incapable."""
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
        profile: ModelProfile = ModelProfile.FAST,
    ) -> tuple[str, Usage]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": self._timeout,
        }
        if self._reasoning_effort and profile is ModelProfile.REASONING:
            # Only the profile it names, so turning thinking down is one variable and not
            # also a change to cataloguing. drop_params discards it where unsupported.
            kwargs["reasoning_effort"] = self._reasoning_effort
        if self._api_key:
            # Explicit every call, so a stale key in os.environ can't silently
            # override the one configured in .env.
            kwargs["api_key"] = self._api_key
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if schema is not None:
            kwargs["response_format"] = schema

        async with self._semaphore:
            response = await _load_litellm().acompletion(**kwargs)

        content = response.choices[0].message.content or ""
        return content, self._usage_of(response, model)

    @staticmethod
    def _usage_of(response: Any, model: str) -> Usage:
        return usage_of(response, model)


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
