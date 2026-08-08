"""Asking a provider what models it will actually sell you, via plain HTTP rather than LiteLLM."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


@dataclass(frozen=True, slots=True)
class ProviderCheck:
    """The result of asking a provider whether it will talk to us."""

    ok: bool
    models: tuple[str, ...] = ()
    error: str = ""

    @property
    def is_auth_failure(self) -> bool:
        return not self.ok and "401" in self.error


def list_models(
    provider_id: str,
    *,
    api_key: str = "",
    api_base: str | None = None,
    headers: dict[str, str] | None = None,
) -> ProviderCheck:
    """Fetch the models this credential can reach; doubles as the credential check. Never raises -- a failure is a message for the user."""
    extra = headers or {}
    try:
        if provider_id == "openai":
            return _openai(api_key, api_base or "https://api.openai.com/v1", extra)
        if provider_id == "anthropic":
            return _anthropic(api_key, extra)
        if provider_id == "custom":
            return _custom(api_key, api_base or "http://localhost:11434/v1", extra)
    except Exception as exc:
        return ProviderCheck(ok=False, error=_explain(exc))
    return ProviderCheck(ok=False, error=f"알 수 없는 프로바이더: {provider_id}")


def _openai(api_key: str, base: str, extra: dict[str, str]) -> ProviderCheck:
    body = _get(f"{base.rstrip('/')}/models", {"Authorization": f"Bearer {api_key}", **extra})
    models = sorted(entry["id"] for entry in body.get("data", []))
    return ProviderCheck(ok=True, models=tuple(models))


def _anthropic(api_key: str, extra: dict[str, str]) -> ProviderCheck:
    body = _get(
        "https://api.anthropic.com/v1/models",
        {"x-api-key": api_key, "anthropic-version": "2023-06-01", **extra},
    )
    models = [entry["id"] for entry in body.get("data", [])]
    return ProviderCheck(ok=True, models=tuple(models))


def _custom(api_key: str, base: str, extra: dict[str, str]) -> ProviderCheck:
    """An OpenAI-shaped endpoint, where listing models is a courtesy rather than a rule.

    vLLM, LM Studio and every gateway in front of them differ on whether `/models`
    exists, whether it needs the same credential, and whether it needs one at all. A
    KT Cloud proxy that served `/chat/completions` perfectly well answered `/models`
    with `401 Not Authenticated - INVALIDCOOKIE`, and setup refused to continue --
    over a listing nothing actually needs. So a failure here is reported and survived:
    the model name gets typed instead.
    """
    request_headers = {**({"Authorization": f"Bearer {api_key}"} if api_key else {}), **extra}
    try:
        body = _get(f"{base.rstrip('/')}/models", request_headers)
    except Exception as exc:
        return ProviderCheck(ok=True, models=(), error=_explain(exc))
    models = sorted(str(entry.get("id", "")) for entry in body.get("data", []) if entry.get("id"))
    return ProviderCheck(ok=True, models=tuple(models))


def _get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return json.loads(response.read())  # type: ignore[no-any-return]


def _explain(exc: Exception) -> str:
    """Turn a provider's failure into a message that names the fix rather than the symptom."""
    if isinstance(exc, urllib.error.HTTPError):
        raw = b""
        try:
            raw = exc.read()
            detail = json.loads(raw).get("error", {})
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
        except Exception:
            # Not every gateway answers in JSON, and the plain-text ones are often the
            # only thing that says what is actually wrong.
            message = raw.decode("utf-8", "replace").strip()[:200]
        # The server's own words, always. A gateway that says
        # "Not Authenticated - INVALIDCOOKIE" is telling you it wants a cookie; reporting
        # that as "키가 거부되었습니다" sends you to check a key that was never the problem.
        said = f'서버 응답: "{message}"' if message else ""
        if exc.code == 401:
            return f"401 인증 실패 — 키나 헤더를 받아주지 않았습니다. {said}".strip()
        if exc.code == 403:
            return f"403 — 인증은 됐지만 이 작업 권한이 없습니다. {said}".strip()
        if exc.code == 404:
            return f"404 — 이 주소에 그런 경로가 없습니다. {said}".strip()
        return f"HTTP {exc.code}. {said}".strip()

    if isinstance(exc, urllib.error.URLError):
        return (
            f"엔드포인트에 연결하지 못했습니다 ({exc.reason}). 로컬 모델이라면 "
            f"실행 중인지, 주소가 맞는지 확인해 주세요."
        )
    return str(exc)[:200]


_PREFERRED_FAST = ("haiku", "nano", "mini", "flash", "8b", "7b", "small")
_PREFERRED_REASONING = ("sonnet", "opus", "gpt-5", "gpt-4.1", "14b", "32b", "70b", "large")


def suggest_models(models: tuple[str, ...]) -> tuple[str, str]:
    """Guess a sensible (fast, reasoning) model pair from a provider's catalogue by matching family-name substrings."""
    if not models:
        return "", ""

    def best(preferences: tuple[str, ...]) -> str:
        for want in preferences:
            for model in models:
                if want in model.lower():
                    return model
        return models[0]

    return best(_PREFERRED_FAST), best(_PREFERRED_REASONING)
