"""What a request body may carry, and what this endpoint turned out not to accept.

Split out of the adapter because it is about the provider's contract rather than about
this program's: every one of these rules was learned from a refusal, and the adapter only
needs to apply them.
"""

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


# Initial operational caps measured against real successful calls. They are safety
# ceilings, not a substitute for schema validation. Unknown extension schemas get the
# conservative general cap rather than running unbounded.
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
    """Merge configured request-body values into a completion call, in place.

    Split because LiteLLM runs with ``drop_params``, which silently discards arguments
    the provider is not known to support -- and that is exactly the set worth
    configuring: ``top_k``, ``min_p``, and the ``chat_template_kwargs`` that turns a
    qwen model's thinking off. Those go through ``extra_body``, which is passed to the
    endpoint untouched. The standard ones stay top-level so LiteLLM can translate them
    per provider.

    ``owns_sampling`` drops the configured sampling values for schema-bound calls, which
    ask a closed question and want the same answer twice. A real configuration carried
    ``temperature: 0.7`` and ``presence_penalty: 1.5`` -- sensible for chat, and a
    presence penalty is the exact opposite of what placement asks for, because placement
    shows the model the existing folder names and wants one of them back. Everything the
    endpoint itself needs, including ``chat_template_kwargs``, still goes through.
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


"""Generation parameters a served model has refused by name, keyed by model.

Process-wide because both adapters talk to the same endpoint: whichever call discovers
the refusal spares every later one. Cleared by nothing -- a served model that starts
accepting a parameter again is a restart away, and the alternative is paying a failed
round trip per call to find out.
"""


def _drop_unsupported(kwargs: dict[str, Any]) -> None:
    """Leave out what this model has already refused."""
    for name in _UNSUPPORTED.get(str(kwargs.get("model", "")), ()):
        kwargs.pop(name, None)


def _learn_refusal(exc: Exception, kwargs: dict[str, Any]) -> str | None:
    """Give up the sampling parameter an endpoint just refused by name, and remember it.

    Read from the error's own ``param`` field rather than matched against a list of model
    names, because the lists disagree with the servers. Measured: gpt-5.6-luna answered
    ``400 unsupported_value`` for ``temperature: 0`` while
    ``litellm.get_supported_openai_params`` listed temperature as supported, so
    ``drop_params`` had no reason to remove it.

    Only generation knobs. Omitting one costs the model's default in its place, which is
    the endpoint's own stated terms; omitting an output cap or a schema would change what
    was asked, and those have their own ladders.
    """
    name = getattr(exc, "param", None)
    if not isinstance(name, str) or name not in _DROPPABLE or name not in kwargs:
        return None
    _UNSUPPORTED.setdefault(str(kwargs.get("model", "")), set()).add(name)
    del kwargs[name]
    return name


def _schema_output_cap(schema: type[BaseModel]) -> int:
    return _SCHEMA_MAX_TOKENS.get(schema.__name__, _DEFAULT_SCHEMA_MAX_TOKENS)
