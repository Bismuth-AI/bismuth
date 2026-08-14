"""Asking a provider what models it will actually sell you, via plain HTTP rather than LiteLLM."""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Catalogue lookup is a one-time setup operation. On the measured Windows host an
# unauthenticated OpenAI request returned its expected 401 after 19.8 seconds, so the
# former 15-second limit rejected a healthy endpoint before its first byte arrived.
# Keep the schema capability probe short; it targets an already-working custom endpoint.
_CATALOG_TIMEOUT = 45.0
_PROBE_TIMEOUT = 15.0


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


_PROBE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "probe",
        "schema": {
            "type": "object",
            "properties": {"ok": {"type": "string"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def supports_response_schema(
    *, api_base: str, model: str, api_key: str = "", headers: dict[str, str] | None = None
) -> bool:
    """Whether this endpoint will constrain decoding to a JSON Schema.

    Worth one call at setup, because the alternative is paid on every call afterwards.
    LiteLLM answers this from a table of models it knows, so anything self-hosted is
    "no" by default and every structured call falls back to describing the schema in the
    prompt and hoping -- which cost two repair turns in eight calls on a 35B model,
    returning eight topics where six were allowed and entities as strings.

    Asked of the endpoint rather than of a table, because that is where the answer is.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 16,
        "response_format": _PROBE_SCHEMA,
    }
    request_headers = {
        "Content-Type": "application/json",
        **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        **(headers or {}),
    }
    try:
        _post(f"{api_base.rstrip('/')}/chat/completions", request_headers, payload)
    except Exception as exc:
        logger.info("%s does not take a json_schema response_format: %s", api_base, _explain(exc))
        return False
    return True


def _get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=_CATALOG_TIMEOUT) as response:
        return json.loads(response.read())  # type: ignore[no-any-return]


def _post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=_PROBE_TIMEOUT) as response:
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

    if isinstance(exc, (TimeoutError, socket.timeout)) or (
        isinstance(exc, urllib.error.URLError)
        and isinstance(exc.reason, (TimeoutError, socket.timeout))
    ):
        return (
            f"모델 목록 응답을 {_CATALOG_TIMEOUT:g}초 동안 기다렸지만 받지 못했습니다. "
            "키 오류라면 보통 401이 즉시 반환되므로 네트워크·프록시·방화벽을 확인해 주세요."
        )

    if isinstance(exc, urllib.error.URLError):
        return (
            f"엔드포인트에 연결하지 못했습니다 ({exc.reason}). 로컬 모델이라면 "
            f"실행 중인지, 주소가 맞는지 확인해 주세요."
        )
    return str(exc)[:200]


_PREFERRED_MODELS = (
    "sonnet",
    "gpt-5",
    "gpt-4.1",
    "32b",
    "35b",
    "14b",
    "large",
    "mini",
    "flash",
)


def suggest_model(models: tuple[str, ...]) -> str:
    """Choose one generally capable model from a provider catalogue."""
    if not models:
        return ""
    for want in _PREFERRED_MODELS:
        for model in models:
            if want in model.lower():
                return model
    return models[0]
