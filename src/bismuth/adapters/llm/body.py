"""Request-body normalization and unsupported-parameter tracking."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


_SAMPLING_PARAMS = frozenset(
    {"temperature", "top_p", "top_k", "min_p", "presence_penalty", "frequency_penalty", "seed"}
)


_DROPPABLE = _SAMPLING_PARAMS | frozenset({"reasoning_effort"})


_UNSUPPORTED: dict[str, set[str]] = {}


# Unknown schemas use the conservative default cap.
_SCHEMA_MAX_TOKENS: dict[str, int] = {
    "CharterDraft": 256,
    "ExistingAssignments": 1024,
    "DensifiedSummary": 512,
    "Members": 512,
    "CardDraft": 2048,
    "CardUpdate": 2048,
    "Emerging": 4096,
}


_DEFAULT_SCHEMA_MAX_TOKENS = 2048


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
        "reasoning_effort",
    }
)
"""Generation settings Bismuth can stop sending without changing the question it asked."""


def apply_body(
    kwargs: dict[str, Any], body: dict[str, Any], *, owns_sampling: bool = False
) -> None:
    """Merge configured values into a completion request.

    Provider-specific values go through ``extra_body``. Schema-bound calls discard
    configured sampling values when ``owns_sampling`` is true.
    """
    if not body:
        return
    extra: dict[str, Any] = dict(kwargs.get("extra_body") or {})
    for name, value in body.items():
        if owns_sampling and name in _SAMPLING_PARAMS:
            continue
        if name in _OPENAI_BODY_PARAMS:
            kwargs[name] = value
        else:
            extra[name] = value
    if extra:
        kwargs["extra_body"] = extra


"""Generation parameters refused by each model during this process."""


def _drop_unsupported(kwargs: dict[str, Any]) -> None:
    """Leave out what this model has already refused."""
    for name in _UNSUPPORTED.get(str(kwargs.get("model", "")), ()):
        kwargs.pop(name, None)


def _learn_refusal(exc: Exception, kwargs: dict[str, Any]) -> str | None:
    """Remove and remember a generation parameter explicitly refused by the endpoint."""
    name = getattr(exc, "param", None)
    if not isinstance(name, str) or name not in _DROPPABLE or name not in kwargs:
        return None
    _UNSUPPORTED.setdefault(str(kwargs.get("model", "")), set()).add(name)
    del kwargs[name]
    return name


def _schema_output_cap(schema: type[BaseModel]) -> int:
    return _SCHEMA_MAX_TOKENS.get(schema.__name__, _DEFAULT_SCHEMA_MAX_TOKENS)
