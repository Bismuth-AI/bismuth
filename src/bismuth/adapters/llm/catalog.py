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
    provider_id: str, *, api_key: str = "", api_base: str | None = None
) -> ProviderCheck:
    """Fetch the models this credential can reach; doubles as the credential check. Never raises -- a failure is a message for the user."""
    try:
        if provider_id == "openai":
            return _openai(api_key, api_base or "https://api.openai.com/v1")
        if provider_id == "anthropic":
            return _anthropic(api_key)
        if provider_id == "ollama":
            return _ollama(api_base or "http://localhost:11434")
        if provider_id == "custom":
            return _openai(api_key or "not-needed", api_base or "http://localhost:8000/v1")
    except Exception as exc:
        return ProviderCheck(ok=False, error=_explain(exc))
    return ProviderCheck(ok=False, error=f"알 수 없는 프로바이더: {provider_id}")


def _openai(api_key: str, base: str) -> ProviderCheck:
    body = _get(f"{base.rstrip('/')}/models", {"Authorization": f"Bearer {api_key}"})
    models = sorted(entry["id"] for entry in body.get("data", []))
    return ProviderCheck(ok=True, models=tuple(models))


def _anthropic(api_key: str) -> ProviderCheck:
    body = _get(
        "https://api.anthropic.com/v1/models",
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    models = [entry["id"] for entry in body.get("data", [])]
    return ProviderCheck(ok=True, models=tuple(models))


def _ollama(base: str) -> ProviderCheck:
    body = _get(f"{base.rstrip('/')}/api/tags", {})
    models = sorted(entry["name"] for entry in body.get("models", []))
    if not models:
        return ProviderCheck(
            ok=False,
            error="Ollama는 켜져 있는데 받아둔 모델이 없습니다. 먼저 받아주세요: ollama pull qwen3:8b",
        )
    return ProviderCheck(ok=True, models=tuple(models))


def _get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return json.loads(response.read())  # type: ignore[no-any-return]


def _explain(exc: Exception) -> str:
    """Turn a provider's failure into a message that names the fix rather than the symptom."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = json.loads(exc.read()).get("error", {})
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
        except Exception:
            message = ""
        if exc.code == 401:
            return f"키가 거부되었습니다. {message}".strip()
        if exc.code == 403:
            return f"유효한 키지만 이 작업 권한이 없습니다. {message}".strip()
        return f"HTTP {exc.code}. {message}".strip()

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
