"""Streaming transport and response parsing helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from bismuth.adapters.llm.body import _learn_refusal

logger = logging.getLogger(__name__)

_litellm: Any = None

_owned_aiohttp_sessions: dict[int, Any] = {}


def _load_litellm() -> Any:
    """Import LiteLLM after application configuration is loaded."""
    global _litellm
    if _litellm is None:
        # Avoid network access while importing. Users may override this setting.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

        import litellm

        litellm.suppress_debug_info = True
        litellm.drop_params = True
        _litellm = litellm
    return _litellm


async def _shared_aiohttp_session() -> Any | None:
    """Return the owned aiohttp session for the current event loop."""
    client = _load_litellm()
    if getattr(client, "__name__", "") != "litellm":
        return None
    loop = asyncio.get_running_loop()
    key = id(loop)
    session = _owned_aiohttp_sessions.get(key)
    if session is None or getattr(session, "closed", False):
        from aiohttp import ClientSession

        session = ClientSession()
        _owned_aiohttp_sessions[key] = session
    return session


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


async def _open_stream(kwargs: dict[str, Any], *, given_up: list[str] | None = None) -> Any:
    """Begin a completion, dropping any sampling parameter the endpoint refuses by name.

    Terminates: a refusal is only acted on while the parameter is still in ``kwargs``, and
    each one is removed before retrying.
    """
    while True:
        try:
            return await _load_litellm().acompletion(**kwargs)
        except Exception as exc:
            name = _learn_refusal(exc, kwargs)
            if name is None:
                raise
            if given_up is not None:
                given_up.append(name)
            logger.warning(
                "%s refused %s (%s); retrying without it",
                kwargs.get("model"),
                name,
                getattr(exc, "code", None) or "400",
            )


class _RepetitionDetectedError(RuntimeError):
    def __init__(self, pattern: str) -> None:
        super().__init__(f"repeated output pattern {pattern!r}")
        self.pattern = pattern


class _InactivityTimeoutError(TimeoutError):
    pass


class _AbsoluteTimeoutError(TimeoutError):
    pass


def _looks_like_timeout(exc: Exception) -> bool:
    names = " ".join(item.__name__ for item in exc.__class__.__mro__)
    return "timeout" in names.casefold() or "timed out" in str(exc).casefold()


def _is_retryable_transport_error(exc: Exception) -> bool:
    if _looks_like_timeout(exc):
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return code in {408, 429, 500, 502, 503, 504}


def _looks_like_repetition(exc: Exception) -> bool:
    """Recognise provider/LiteLLM repetition errors through wrapper exceptions."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = f"{type(current).__name__}: {current}".casefold()
        if "repeating the same chunk" in message or "repeated stream chunk" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


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
    """Serialize a stream chunk without emitting adapter-library warnings."""
    dump = getattr(chunk, "model_dump", None)
    if dump is not None:
        return dump(mode="json", warnings=False)
    if isinstance(chunk, dict):
        return chunk
    return str(chunk)


def _parse_json(raw: str) -> Any:
    """Recover a JSON object from plain, fenced, or prose-prefixed output.

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
    """Parse the first brace-balanced JSON object while respecting strings."""
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


def preload() -> None:
    """Load LiteLLM after configuration and before the first request."""
    _load_litellm()


async def close_clients() -> None:
    """Close LiteLLM's shared async transports during application shutdown."""
    sessions = list(_owned_aiohttp_sessions.values())
    _owned_aiohttp_sessions.clear()
    for session in sessions:
        if not getattr(session, "closed", True):
            await session.close()
    if _litellm is None:
        return
    close = getattr(_litellm, "close_litellm_async_clients", None)
    if close is not None:
        await close()
