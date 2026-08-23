"""Provider capability checks over HTTP."""

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
    return ProviderCheck(ok=False, error=f"Unknown provider: {provider_id}")


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
    """Query an OpenAI-compatible endpoint without requiring model listing support."""
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
    """Return whether the endpoint enforces a JSON Schema response format."""
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
        response = _post(f"{api_base.rstrip('/')}/chat/completions", request_headers, payload)
    except Exception as exc:
        logger.info("%s does not take a json_schema response_format: %s", api_base, _explain(exc))
        return False
    return _probe_succeeded(response)


def _probe_succeeded(response: dict[str, Any]) -> bool:
    try:
        message = response["choices"][0]["message"]
        content = message.get("parsed", message.get("content"))
        parsed = json.loads(content) if isinstance(content, str) else content
        return isinstance(parsed, dict) and isinstance(parsed.get("ok"), str)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return False


def _get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return json.loads(response.read())  # type: ignore[no-any-return]


def _post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return json.loads(response.read())  # type: ignore[no-any-return]


def _explain(exc: Exception) -> str:
    """Convert a provider error into a concise user-facing message."""
    if isinstance(exc, urllib.error.HTTPError):
        raw = b""
        try:
            raw = exc.read()
            detail = json.loads(raw).get("error", {})
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
        except Exception:
            message = raw.decode("utf-8", "replace").strip()[:200]
        said = f'Provider response: "{message}"' if message else ""
        if exc.code == 401:
            return f"401 authentication failed. Check the API key and headers. {said}".strip()
        if exc.code == 403:
            return f"403 permission denied. {said}".strip()
        if exc.code == 404:
            return f"404 endpoint not found. {said}".strip()
        return f"HTTP {exc.code}. {said}".strip()

    if isinstance(exc, urllib.error.URLError):
        return f"Could not connect to the endpoint ({exc.reason}). Check its address and status."
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
