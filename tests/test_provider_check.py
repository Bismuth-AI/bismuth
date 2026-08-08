"""Reaching a model endpoint that is not one of the two hosted ones.

An OpenAI-compatible address can be vLLM, Ollama, LM Studio or a corporate gateway in
front of any of them, and they disagree about everything except `/chat/completions`.
Setup has to survive that disagreement, because the endpoint works regardless.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from bismuth.adapters.llm import catalog
from bismuth.config import PROVIDERS, provider


class TestProviders:
    def test_the_three_that_are_offered(self) -> None:
        """Ollama is not one of them: it speaks the OpenAI protocol, so it is reached
        through the compatible option like everything else that does."""
        assert [p.id for p in PROVIDERS] == ["anthropic", "openai", "custom"]
        assert provider("ollama") is None


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body))  # type: ignore[arg-type]


class TestTheMessage:
    def test_the_servers_own_words_survive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A gateway saying INVALIDCOOKIE is telling you it wants a cookie. Reporting
        that as "키가 거부되었습니다" sends you to check a key that was never the problem."""
        monkeypatch.setattr(
            catalog, "_get", lambda *_: (_ for _ in ()).throw(_http_error(401, b"INVALIDCOOKIE"))
        )

        check = catalog.list_models("openai", api_key="k")

        assert not check.ok
        assert "INVALIDCOOKIE" in check.error

    def test_a_plain_text_body_is_not_thrown_away(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not every gateway answers in JSON, and the plain ones often say the most."""
        monkeypatch.setattr(
            catalog, "_get", lambda *_: (_ for _ in ()).throw(_http_error(404, b"no such route"))
        )

        assert "no such route" in catalog.list_models("openai", api_key="k").error


class TestCompatibleEndpoint:
    def test_a_missing_catalogue_does_not_block_setup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured: a proxy served /chat/completions perfectly well and answered
        /models with 401, and setup refused to continue -- over a listing that nothing
        in the pipeline needs."""
        monkeypatch.setattr(
            catalog, "_get", lambda *_: (_ for _ in ()).throw(_http_error(401, b"INVALIDCOOKIE"))
        )

        check = catalog.list_models("custom", api_base="https://gateway/v1")

        assert check.ok  # the model name gets typed instead
        assert check.models == ()
        assert "INVALIDCOOKIE" in check.error

    def test_extra_headers_are_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The only way to reach a gateway that authenticates with a cookie."""
        seen: dict[str, Any] = {}

        def fake_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
            seen["url"], seen["headers"] = url, headers
            return {"data": [{"id": "qwen3.6-35b"}]}

        monkeypatch.setattr(catalog, "_get", fake_get)

        check = catalog.list_models(
            "custom", api_base="https://gateway/v1", headers={"Cookie": "session=abc"}
        )

        assert check.models == ("qwen3.6-35b",)
        assert seen["headers"]["Cookie"] == "session=abc"
        assert seen["url"] == "https://gateway/v1/models"

    def test_a_credential_is_sent_as_a_bearer_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A compatible endpoint may still want a key -- vLLM's --api-key, a LiteLLM
        proxy. "Not required" is not "cannot be given"."""
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            catalog, "_get", lambda url, headers: seen.update(headers) or {"data": []}
        )

        catalog.list_models("custom", api_key="sk-secret", api_base="https://gateway/v1")

        assert seen["Authorization"] == "Bearer sk-secret"

    def test_no_credential_means_no_authorization_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It used to send `Bearer not-needed`, which some gateways reject outright."""
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            catalog, "_get", lambda url, headers: seen.update(headers) or {"data": []}
        )

        catalog.list_models("custom", api_base="https://gateway/v1")

        assert "Authorization" not in seen


class TestSetupApi:
    def test_headers_are_saved_and_returned(self, client) -> None:  # type: ignore[no-untyped-def]
        state = client.get("/api/setup").json()
        assert state["api_headers"] == {}

    def test_the_check_passes_headers_through(self, client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        seen: dict[str, Any] = {}

        def fake_list(provider_id: str, **kwargs: Any) -> catalog.ProviderCheck:
            seen.update(kwargs)
            return catalog.ProviderCheck(ok=True, models=("m",))

        monkeypatch.setattr("bismuth.api.app.list_models", fake_list)

        response = client.post(
            "/api/setup/check",
            json={
                "provider_id": "custom",
                "api_base": "https://gateway/v1",
                "api_headers": {"Cookie": "session=abc"},
            },
        )

        assert response.status_code == 200, response.text
        assert seen["headers"] == {"Cookie": "session=abc"}


def test_a_json_error_body_still_reads_well(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"error": {"message": "Incorrect API key provided"}}).encode()
    monkeypatch.setattr(catalog, "_get", lambda *_: (_ for _ in ()).throw(_http_error(401, body)))

    assert "Incorrect API key" in catalog.list_models("openai", api_key="bad").error
