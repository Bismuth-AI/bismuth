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

from bismuth.adapters.llm import LiteLLMAdapter, catalog
from bismuth.adapters.llm.litellm_adapter import apply_body
from bismuth.config import PROVIDERS, Settings, provider


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


class TestSecrets:
    def test_a_header_is_redacted_like_a_key(self) -> None:
        """Headers exist because a bearer token was not enough, so whatever is in them
        is a credential too. The first one anybody wrote was a session cookie, and it
        went into bismuth.log in full."""
        redacted = Settings(
            api_key="sk-supersecret",
            api_headers={"Cookie": "appproxy_permit=ZjAyMTE0YmJiOTBmNmRkZA=="},
        ).redacted()

        assert "supersecret" not in str(redacted)
        assert "ZjAyMTE0" not in str(redacted)
        assert redacted["api_headers"] == {"Cookie": "…ZA=="}  # still tells two apart


class TestRequestBody:
    """A server can want values Bismuth has no opinion about, and LiteLLM drops the
    interesting ones on the floor unless they are smuggled past it."""

    def test_standard_values_stay_top_level(self) -> None:
        """LiteLLM translates these per provider, so it has to see them itself."""
        kwargs: dict[str, Any] = {"model": "m", "temperature": 0.0}

        apply_body(kwargs, {"temperature": 0.7, "top_p": 0.8, "presence_penalty": 1.5})

        assert kwargs["temperature"] == 0.7  # configured wins over our determinism default
        assert kwargs["top_p"] == 0.8
        assert "extra_body" not in kwargs

    def test_everything_else_goes_through_extra_body(self) -> None:
        """drop_params discards arguments the provider is not known to support, which is
        exactly the set worth configuring. extra_body reaches the endpoint untouched."""
        kwargs: dict[str, Any] = {"model": "m"}

        apply_body(
            kwargs,
            {"top_k": 20, "min_p": 0.0, "chat_template_kwargs": {"enable_thinking": False}},
        )

        assert kwargs["extra_body"] == {
            "top_k": 20,
            "min_p": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def test_a_mixed_body_is_split(self) -> None:
        kwargs: dict[str, Any] = {"model": "m", "temperature": 0.0}

        apply_body(kwargs, {"temperature": 0.7, "top_k": 20})

        assert kwargs["temperature"] == 0.7
        assert kwargs["extra_body"] == {"top_k": 20}

    def test_nothing_configured_changes_nothing(self) -> None:
        kwargs: dict[str, Any] = {"model": "m", "temperature": 0.0}

        apply_body(kwargs, {})

        assert kwargs == {"model": "m", "temperature": 0.0}

    def test_the_adapter_sends_it(self) -> None:
        """The whole point: a qwen model with thinking left on took 93 seconds a
        document instead of 6."""
        adapter = LiteLLMAdapter(
            model_fast="openai/qwen3.6-35b",
            model_reasoning="openai/qwen3.6-35b",
            body={"chat_template_kwargs": {"enable_thinking": False}, "top_p": 0.8},
        )

        assert adapter._body["top_p"] == 0.8
        kwargs: dict[str, Any] = {}
        apply_body(kwargs, adapter._body)
        assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


class TestSchemaSupport:
    """LiteLLM answers "does this model take a json_schema?" from a table of models it
    knows, so a self-hosted endpoint is always no -- and every structured call then
    falls back to describing the schema in the prompt and repairing the reply."""

    def test_an_endpoint_that_takes_a_schema(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def fake_post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
            seen.update(url=url, headers=headers, payload=payload)
            return {"choices": []}

        monkeypatch.setattr(catalog, "_post", fake_post)

        assert catalog.supports_response_schema(
            api_base="https://gateway/v1",
            model="qwen3.6-35b",
            api_key="sk-x",
            headers={"Cookie": "c"},
        )
        assert seen["url"] == "https://gateway/v1/chat/completions"
        assert seen["payload"]["response_format"]["type"] == "json_schema"
        # Both credentials: the gateway wants the cookie, the model server the bearer.
        assert seen["headers"]["Authorization"] == "Bearer sk-x"
        assert seen["headers"]["Cookie"] == "c"

    def test_an_endpoint_that_refuses_is_a_no_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            catalog,
            "_post",
            lambda *_: (_ for _ in ()).throw(_http_error(400, b"unsupported response_format")),
        )

        assert not catalog.supports_response_schema(api_base="https://g/v1", model="m")

    def test_the_adapter_obeys_the_setting_over_the_table(self) -> None:
        forced = LiteLLMAdapter(
            model_fast="openai/qwen3.6-35b", model_reasoning="openai/q", native_schema=True
        )
        refused = LiteLLMAdapter(
            model_fast="openai/gpt-4o", model_reasoning="openai/gpt-4o", native_schema=False
        )

        assert forced._supports_native_schema("openai/qwen3.6-35b") is True
        assert refused._supports_native_schema("openai/gpt-4o") is False
