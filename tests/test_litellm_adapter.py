"""LiteLLMAdapter logic against a stubbed litellm: fallback ladder, JSON salvage, repair, explicit key."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

from bismuth.adapters.llm import litellm_adapter
from bismuth.adapters.llm.litellm_adapter import _MAX_CHOICE_MAX_TOKENS, LiteLLMAdapter, _parse_json
from bismuth.domain.errors import ModelRequestError, StructuredOutputError
from bismuth.ports.llm import Prompt


class Answer(BaseModel):
    name: str
    year: int


class Emerging(BaseModel):
    emerged: bool


class FakeUsage:
    prompt_tokens = 11
    completion_tokens = 22


class FakeResponse:
    def __init__(self, content: str) -> None:
        message = type("M", (), {"content": content})()
        self.choices = [type("C", (), {"message": message})()]
        self.usage = FakeUsage()


class FakeDelta:
    def __init__(self, content: str = "", reasoning_content: str = "") -> None:
        self.content = content
        self.reasoning_content = reasoning_content


class FakeChunk:
    def __init__(
        self, content: str = "", *, reasoning_content: str = "", finish_reason: str | None = None
    ) -> None:
        delta = FakeDelta(content, reasoning_content)
        self.choices = [type("Choice", (), {"delta": delta, "finish_reason": finish_reason})()]
        self.usage = FakeUsage() if finish_reason else None

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        choice = self.choices[0]
        return {
            "choices": [
                {
                    "delta": {
                        "content": choice.delta.content,
                        "reasoning_content": choice.delta.reasoning_content,
                    },
                    "finish_reason": choice.finish_reason,
                }
            ],
            "usage": (
                {
                    "prompt_tokens": self.usage.prompt_tokens,
                    "completion_tokens": self.usage.completion_tokens,
                }
                if self.usage
                else None
            ),
        }


class FakeStream:
    def __init__(self, reply: str, *, finish_reason: str = "stop") -> None:
        middle = max(1, len(reply) // 2)
        self._chunks = iter(
            [
                FakeChunk(reply[:middle]),
                FakeChunk(reply[middle:]),
                FakeChunk(finish_reason=finish_reason),
            ]
        )

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> FakeChunk:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FailingStream:
    def __init__(self) -> None:
        self._sent = False

    def __aiter__(self) -> FailingStream:
        return self

    async def __anext__(self) -> FakeChunk:
        if not self._sent:
            self._sent = True
            return FakeChunk('{"name": "partial')
        raise TimeoutError("no next chunk")


class ProviderRepetitionStream:
    def __init__(self) -> None:
        self._sent = False

    def __aiter__(self) -> ProviderRepetitionStream:
        return self

    async def __anext__(self) -> FakeChunk:
        if not self._sent:
            self._sent = True
            return FakeChunk('{"name": "partial')
        raise RuntimeError("The model is repeating the same chunk = '   '")


class WhitespaceLoopStream:
    def __init__(self) -> None:
        self._chunks = 0

    def __aiter__(self) -> WhitespaceLoopStream:
        return self

    async def __anext__(self) -> FakeChunk:
        self._chunks += 1
        if self._chunks == 1:
            return FakeChunk('{"name": "partial')
        return FakeChunk(" " * 64)


class StubLiteLLM:
    """Stands in for litellm; records the calls it receives."""

    def __init__(self, replies: list[str], *, native: bool = True) -> None:
        self._replies = replies
        self._native = native
        self.calls: list[dict[str, Any]] = []
        self.suppress_debug_info = False
        self.drop_params = False

    async def acompletion(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        return FakeStream(self._replies[min(len(self.calls) - 1, len(self._replies) - 1)])

    def stream_chunk_builder(
        self, chunks: list[FakeChunk], messages: list[dict[str, Any]] | None = None
    ) -> FakeResponse:
        return FakeResponse("".join(chunk.choices[0].delta.content for chunk in chunks))

    def supports_response_schema(self, model: str) -> bool:
        return self._native

    def completion_cost(self, completion_response: Any) -> float:
        return 0.00001

    def validate_environment(self, model: str) -> dict[str, Any]:
        return {"keys_in_environment": True, "missing_keys": []}


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> StubLiteLLM:
    def install(replies: list[str], *, native: bool = True) -> StubLiteLLM:
        fake = StubLiteLLM(replies, native=native)
        monkeypatch.setattr(litellm_adapter, "_litellm", fake)
        return fake

    return install  # type: ignore[return-value]


def adapter(**kwargs: Any) -> LiteLLMAdapter:
    defaults = {"model": "openai/model"}
    return LiteLLMAdapter(**{**defaults, **kwargs})  # type: ignore[arg-type]


class TestStructured:
    async def test_returns_a_validated_instance(self, stub: Any) -> None:
        stub(['{"name": "Apollo", "year": 2023}'])
        result = await adapter().structured(Prompt(system="s", user="u"), schema=Answer)
        assert result == Answer(name="Apollo", year=2023)

    async def test_usage_is_recorded_without_mutating_a_frozen_model(self, stub: Any) -> None:
        stub(['{"name": "Apollo", "year": 2023}'])
        engine = adapter()
        await engine.structured(Prompt(system="s", user="u"), schema=Answer)

        usage = engine.drain_usage()
        assert len(usage) == 1
        assert usage[0].input_tokens == 11
        assert usage[0].retries == 0
        assert engine.drain_usage() == []

    async def test_the_selected_model_is_used(self, stub: Any) -> None:
        fake = stub(['{"name": "a", "year": 1}'])
        engine = adapter()
        await engine.structured(Prompt(system="s", user="u"), schema=Answer)
        assert fake.calls[0]["model"] == "openai/model"

    async def test_transport_retries_are_disabled(self, stub: Any) -> None:
        fake = stub(['{"name": "a", "year": 1}'])
        await adapter().structured(Prompt(system="s", user="u"), schema=Answer)
        assert fake.calls[0]["max_retries"] == 0

    async def test_schema_specific_cap_is_sent(self, stub: Any) -> None:
        fake = stub(['{"emerged": false}'])
        await adapter().structured(Prompt(system="s", user="u"), schema=Emerging)
        assert fake.calls[0]["max_tokens"] == 4096

    async def test_config_can_lower_but_not_raise_a_schema_cap(self, stub: Any) -> None:
        lowered = stub(['{"emerged": false}'])
        await adapter(body={"max_tokens": 64}).structured(
            Prompt(system="s", user="u"), schema=Emerging
        )
        assert lowered.calls[0]["max_tokens"] == 64

    async def test_every_raw_stream_chunk_is_kept(
        self, stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = stub(['{"name": "a", "year": 1}'])
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(litellm_adapter, "log_llm_call", records.append)
        await adapter().structured(Prompt(system="s", user="u"), schema=Answer)

        assert fake.calls[0]["stream"] is True
        assert fake.calls[0]["stream_options"] == {"include_usage": True}
        stream = records[-1]["attempts"][0]["stream"]
        assert len(stream["chunks"]) == 3
        assert stream["content"] == '{"name": "a", "year": 1}'
        assert stream["finish_reason"] == "stop"
        assert stream["completed"] is True
        assert all("raw" in chunk and "gap_ms" in chunk for chunk in stream["chunks"])

    async def test_timeout_keeps_every_chunk_received_before_it(
        self, stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = stub(['{"unused": true}'])

        async def fail_after_one_chunk(**kwargs: Any) -> FailingStream:
            fake.calls.append(kwargs)
            return FailingStream()

        fake.acompletion = fail_after_one_chunk  # type: ignore[method-assign]
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(litellm_adapter, "log_llm_call", records.append)

        with pytest.raises(ModelRequestError, match="제한시간"):
            await adapter().structured(Prompt(system="s", user="u"), schema=Answer)

        attempt = records[-1]["attempts"][0]
        assert attempt["stream"]["content"] == '{"name": "partial'
        assert attempt["stream"]["completed"] is False
        assert len(attempt["stream"]["chunks"]) == 1
        assert "no next chunk" in attempt["transport_error"]


class TestExplicitKey:
    """The key is an argument, never something LiteLLM goes looking for."""

    async def test_the_key_is_passed_on_every_call(self, stub: Any) -> None:
        fake = stub(['{"name": "a", "year": 1}'])
        await adapter(api_key="sk-ours").structured(Prompt(system="s", user="u"), schema=Answer)
        assert fake.calls[0]["api_key"] == "sk-ours"

    async def test_no_key_is_sent_when_there_is_none(self, stub: Any) -> None:
        # Sending an empty key is not the same as sending none.
        fake = stub(['{"name": "a", "year": 1}'])
        await adapter().structured(Prompt(system="s", user="u"), schema=Answer)
        assert "api_key" not in fake.calls[0]

    async def test_the_endpoint_is_passed_when_set(self, stub: Any) -> None:
        fake = stub(['{"name": "a", "year": 1}'])
        await adapter(api_base="http://localhost:11434").structured(
            Prompt(system="s", user="u"), schema=Answer
        )
        assert fake.calls[0]["api_base"] == "http://localhost:11434"

    async def test_the_key_and_gateway_headers_never_reach_the_log(
        self, stub: Any, monkeypatch: Any
    ) -> None:
        """The parameters are logged from the call kwargs, which is where the key lives."""
        stub(['{"name": "a", "year": 1}'])
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(litellm_adapter, "log_llm_call", records.append)

        await adapter(api_key="sk-ours", headers={"Cookie": "session=secret"}).structured(
            Prompt(system="s", user="u"), schema=Answer
        )

        written = json.dumps(records[-1], default=str)
        assert "sk-ours" not in written
        assert "session=secret" not in written


class TestRecordedParameters:
    """Sampling cannot be ruled in or out of a failure that never recorded it."""

    async def test_an_attempt_records_the_settings_it_ran_with(
        self, stub: Any, monkeypatch: Any
    ) -> None:
        stub(['{"name": "a", "year": 1}'])
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(litellm_adapter, "log_llm_call", records.append)

        await adapter(
            body={
                "temperature": 0.7,
                "top_k": 20,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).structured(Prompt(system="s", user="u"), schema=Answer)

        parameters = records[-1]["attempts"][0]["request_parameters"]
        # Bismuth owns sampling for a schema-bound call; the endpoint's own needs still go.
        assert parameters["temperature"] == 0.0
        assert "top_k" not in parameters.get("extra_body", {})
        assert parameters["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert parameters["max_tokens"]

    async def test_the_agent_path_keeps_the_operators_sampling(self, stub: Any) -> None:
        """Only closed questions are Bismuth's to make deterministic."""
        kwargs: dict[str, Any] = {}
        litellm_adapter.apply_body(kwargs, {"temperature": 0.7, "top_k": 20})
        assert kwargs["temperature"] == 0.7
        assert kwargs["extra_body"]["top_k"] == 20

    async def test_a_repair_turn_records_its_own_settings(
        self, stub: Any, monkeypatch: Any
    ) -> None:
        """A retry can change the cap and drop native enforcement; per-call is not enough."""
        stub(["not json at all", '{"name": "a", "year": 1}'])
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(litellm_adapter, "log_llm_call", records.append)

        await adapter().structured(Prompt(system="s", user="u"), schema=Answer)

        attempts = records[-1]["attempts"]
        assert len(attempts) == 2
        assert all("request_parameters" in attempt for attempt in attempts)
        # The repair turn is a different input, and the log has to show which.
        assert attempts[1]["messages"] != attempts[0]["messages"]


class TestClientCleanup:
    async def test_owned_shared_sessions_are_closed(self, stub: Any) -> None:
        stub(['{"name": "a", "year": 1}'])

        class Session:
            closed = False

            async def close(self) -> None:
                self.closed = True

        session = Session()
        litellm_adapter._owned_aiohttp_sessions[1] = session

        await litellm_adapter.close_clients()

        assert session.closed
        assert litellm_adapter._owned_aiohttp_sessions == {}


class TestStructuredOutputTiers:
    async def test_a_capable_provider_gets_the_schema_as_a_parameter(self, stub: Any) -> None:
        fake = stub(['{"name": "a", "year": 1}'], native=True)
        await adapter().structured(Prompt(system="s", user="u"), schema=Answer)
        assert fake.calls[0]["response_format"] is Answer
        assert "JSON Schema" not in fake.calls[0]["messages"][0]["content"]

    async def test_an_incapable_provider_gets_the_schema_in_the_prompt(self, stub: Any) -> None:
        fake = stub(['{"name": "a", "year": 1}'], native=False)
        await adapter().structured(Prompt(system="s", user="u"), schema=Answer)
        assert "response_format" not in fake.calls[0]
        assert "JSON Schema" in fake.calls[0]["messages"][0]["content"]

    async def test_a_cache_hint_marks_the_system_prompt(self, stub: Any) -> None:
        fake = stub(['{"name": "a", "year": 1}'])
        await adapter().structured(Prompt(system="s", user="u", cache_hint=True), schema=Answer)
        assert fake.calls[0]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


class TestRepair:
    async def test_a_validation_failure_is_fed_back_and_fixed(self, stub: Any) -> None:
        fake = stub(['{"name": "a", "year": "twenty"}', '{"name": "a", "year": 20}'])
        result = await adapter(max_schema_retries=1).structured(
            Prompt(system="s", user="u"), schema=Answer
        )

        assert result.year == 20
        assert len(fake.calls) == 2
        repair = fake.calls[1]["messages"][-1]["content"]
        assert "did not validate" in repair
        assert "year" in repair

    async def test_retries_are_counted(self, stub: Any) -> None:
        stub(['{"bad": 1}', '{"name": "a", "year": 1}'])
        engine = adapter(max_schema_retries=1)
        await engine.structured(Prompt(system="s", user="u"), schema=Answer)
        assert [u.retries for u in engine.drain_usage()] == [0, 1]

    async def test_full_bad_output_is_logged_but_not_copied_into_repair_context(
        self, stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = "not-json:" + "".join(f"{index:04x}" for index in range(1250))
        fake = stub([bad, '{"name": "a", "year": 1}'])
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(litellm_adapter, "log_llm_call", records.append)

        await adapter(max_schema_retries=1).structured(Prompt(system="s", user="u"), schema=Answer)

        assert records[-1]["attempts"][0]["raw"] == bad
        repair_messages = fake.calls[1]["messages"]
        assert all(message["role"] != "assistant" for message in repair_messages)
        assert bad[:2000] in repair_messages[-1]["content"]
        assert bad[:2001] not in repair_messages[-1]["content"]

    async def test_giving_up_says_what_to_do(self, stub: Any) -> None:
        stub(["not json at all"])
        with pytest.raises(StructuredOutputError, match="Inspect finish_reason"):
            await adapter(max_schema_retries=0).structured(
                Prompt(system="s", user="u"), schema=Answer
            )

    async def test_length_finish_retries_cleanly_with_a_larger_budget(self, stub: Any) -> None:
        fake = stub(['{"name": "Apollo", "year": 2023}'])

        async def first_is_cut_short(**kwargs: Any) -> FakeStream:
            fake.calls.append(kwargs)
            if len(fake.calls) == 1:
                return FakeStream('{"name": "Apollo"', finish_reason="length")
            return FakeStream('{"name": "Apollo", "year": 2023}')

        fake.acompletion = first_is_cut_short  # type: ignore[method-assign]
        result = await adapter(max_schema_retries=1).structured(
            Prompt(system="s", user="u"), schema=Answer
        )

        assert result.year == 2023
        assert [call["max_tokens"] for call in fake.calls] == [2048, 4096]
        # The retry says what went wrong. Doubling the budget in silence is fuel when the
        # cause is a model enumerating: one card filled 2048, 4096 and 8192 in turn.
        assert len(fake.calls[1]["messages"]) == 3
        assert "ran past the generation limit" in fake.calls[1]["messages"][-1]["content"]

    async def test_a_configured_lower_limit_is_not_retried_unchanged(self, stub: Any) -> None:
        fake = stub(['{"name": "Apollo"'])

        async def always_cut(**kwargs: Any) -> FakeStream:
            fake.calls.append(kwargs)
            return FakeStream('{"name": "Apollo"', finish_reason="length")

        fake.acompletion = always_cut  # type: ignore[method-assign]
        with pytest.raises(StructuredOutputError, match="generation limit"):
            await adapter(body={"max_tokens": 64}, max_schema_retries=2).structured(
                Prompt(system="s", user="u"), schema=Answer
            )

        assert len(fake.calls) == 1

    async def test_litellm_wrapped_repetition_gets_a_clean_non_native_retry(
        self, stub: Any
    ) -> None:
        fake = stub(['{"name": "Apollo", "year": 2023}'])

        async def repeat_then_answer(**kwargs: Any):  # type: ignore[no-untyped-def]
            fake.calls.append(kwargs)
            if len(fake.calls) == 1:
                return ProviderRepetitionStream()
            return FakeStream('{"name": "Apollo", "year": 2023}')

        fake.acompletion = repeat_then_answer  # type: ignore[method-assign]
        result = await adapter(max_schema_retries=1).structured(
            Prompt(system="original", user="evidence"), schema=Answer
        )

        assert result.year == 2023
        assert "response_format" in fake.calls[0]
        assert "response_format" not in fake.calls[1]
        assert len(fake.calls[1]["messages"]) == 2
        assert "partial" not in str(fake.calls[1]["messages"])

    async def test_whitespace_only_progress_is_stopped_before_the_provider_limit(
        self, stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = stub(['{"name": "Apollo", "year": 2023}'])

        async def loop_then_answer(**kwargs: Any):  # type: ignore[no-untyped-def]
            fake.calls.append(kwargs)
            if len(fake.calls) == 1:
                return WhitespaceLoopStream()
            return FakeStream('{"name": "Apollo", "year": 2023}')

        fake.acompletion = loop_then_answer  # type: ignore[method-assign]
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(litellm_adapter, "log_llm_call", records.append)

        result = await adapter(max_schema_retries=1).structured(
            Prompt(system="s", user="u"), schema=Answer
        )

        assert result.year == 2023
        abort = records[-1]["attempts"][0]["stream"]["abort"]
        assert abort["pattern"] == "<whitespace>"
        assert abort["after_chars"] < 1000


class TestJsonSalvage:
    """What tier-3 providers actually send back."""

    def test_plain_json(self) -> None:
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_markdown_fences(self) -> None:
        # The single most common reply from a model with no JSON mode.
        assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_prose_before_the_object(self) -> None:
        assert _parse_json('Sure! Here is the JSON:\n{"a": 1}') == {"a": 1}

    def test_a_brace_inside_a_string_does_not_end_the_scan(self) -> None:
        # A naive find('{')/rfind('}') scan would break here.
        assert _parse_json('here: {"code": "if (x) { y() }", "n": 1}') == {
            "code": "if (x) { y() }",
            "n": 1,
        }

    def test_an_escaped_quote_does_not_end_the_string(self) -> None:
        assert _parse_json(r'{"q": "she said \"hi\" }", "n": 2}') == {
            "q": 'she said "hi" }',
            "n": 2,
        }

    @pytest.mark.parametrize("junk", ["", "   ", "no json here", "{unclosed"])
    def test_hopeless_replies_raise(self, junk: str) -> None:
        with pytest.raises(ValueError):
            _parse_json(junk)


class TestPlainChoice:
    async def test_choice_is_plain_bounded_and_deterministic(self, stub: Any) -> None:
        fake = stub(["F002"])
        result = await adapter(body={"temperature": 0.7, "max_tokens": 999}).choose(
            Prompt(system="s", user="u"),
            choices=["F001", "F002", "STAY", "UNREADABLE"],
        )

        assert result == "F002"
        request = fake.calls[0]
        assert "response_format" not in request
        assert request["temperature"] == 0.0
        assert request["max_tokens"] == 32
        assert request["max_retries"] == 0

    async def test_invalid_reply_gets_one_clean_retry(self, stub: Any) -> None:
        bad = '{"folder_id":"F002"}'
        fake = stub([bad, "F002"])

        result = await adapter().choose(
            Prompt(system="original system", user="document facts"),
            choices=["F001", "F002", "STAY", "UNREADABLE"],
        )

        assert result == "F002"
        assert len(fake.calls) == 2
        retry = fake.calls[1]["messages"]
        assert all(bad not in str(message["content"]) for message in retry)
        assert retry[0]["content"] == litellm_adapter._CHOICE_RETRY_SYSTEM

    async def test_two_invalid_replies_fail_closed(self, stub: Any) -> None:
        stub(["the answer is F002", '"F002"'])
        with pytest.raises(StructuredOutputError, match="one allowed choice"):
            await adapter().choose(Prompt(system="s", user="u"), choices=["F001", "F002", "STAY"])

    async def test_repetition_is_aborted_then_retried_cleanly(
        self, stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub(["}]}" * 1000, "F002"])
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(litellm_adapter, "log_llm_call", records.append)

        result = await adapter().choose(
            Prompt(system="s", user="u"), choices=["F001", "F002", "STAY"]
        )

        assert result == "F002"
        first = records[-1]["attempts"][0]["stream"]
        assert first["abort"]["kind"] == "repetition"
        assert first["abort"]["after_chars"] < 3000
        assert first["content"]


class TestAbsoluteDeadline:
    async def test_continuous_generation_cannot_run_past_absolute_deadline(
        self, stub: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = stub(['{"unused": true}'])

        class SlowStream:
            def __aiter__(self) -> SlowStream:
                return self

            async def __anext__(self) -> FakeChunk:
                await asyncio.sleep(0.02)
                return FakeChunk("x")

        async def stream_forever(**kwargs: Any) -> SlowStream:
            fake.calls.append(kwargs)
            return SlowStream()

        fake.acompletion = stream_forever  # type: ignore[method-assign]
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(litellm_adapter, "log_llm_call", records.append)

        with pytest.raises(ModelRequestError, match="제한시간"):
            await adapter(timeout=1.0, absolute_timeout=0.01).structured(
                Prompt(system="s", user="u"), schema=Answer
            )

        abort = records[-1]["attempts"][0]["stream"]["abort"]
        assert abort["kind"] == "absolute_timeout"


class RefusesOneParameter(StubLiteLLM):
    """An endpoint that answers 400 for a sampling parameter by name, then works.

    Shaped like the real thing: OpenAI reports the offending parameter in ``param`` and
    the reason in ``code``, and LiteLLM passes both through on BadRequestError. Measured
    against gpt-5.6-luna, which refused ``temperature: 0`` with ``unsupported_value``
    while litellm.get_supported_openai_params listed temperature as supported -- so
    drop_params had no reason to remove it and every call failed.
    """

    def __init__(self, replies: list[str], *, refuses: str) -> None:
        super().__init__(replies)
        self._refuses = refuses

    async def acompletion(self, **kwargs: Any) -> FakeStream:
        if self._refuses in kwargs:
            self.calls.append(kwargs)
            error = Exception(f"Unsupported value: '{self._refuses}' is not supported")
            error.param = self._refuses  # type: ignore[attr-defined]
            error.code = "unsupported_value"  # type: ignore[attr-defined]
            raise error
        return await super().acompletion(**kwargs)


class TestAParameterTheEndpointRefuses:
    @pytest.fixture(autouse=True)
    def _forget_between_tests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The registry is process-wide on purpose, so a test must not leak into the next."""
        monkeypatch.setattr(litellm_adapter, "_UNSUPPORTED", {})

    async def test_the_call_gives_the_parameter_up_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = RefusesOneParameter(['{"name": "Apollo", "year": 2023}'], refuses="temperature")
        monkeypatch.setattr(litellm_adapter, "_litellm", fake)

        result = await adapter().structured(Prompt(system="s", user="u"), schema=Answer)

        assert result == Answer(name="Apollo", year=2023)
        assert "temperature" in fake.calls[0]
        assert "temperature" not in fake.calls[1]

    async def test_later_calls_do_not_pay_the_refusal_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A served model's terms do not change between two documents."""
        fake = RefusesOneParameter(['{"name": "Apollo", "year": 2023}'] * 2, refuses="temperature")
        monkeypatch.setattr(litellm_adapter, "_litellm", fake)
        engine = adapter()

        await engine.structured(Prompt(system="s", user="u"), schema=Answer)
        await engine.structured(Prompt(system="s", user="u"), schema=Answer)

        assert len(fake.calls) == 3  # one refusal, then two accepted calls

    async def test_the_record_says_what_was_given_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sampling change that no log mentions makes the next run unexplainable."""
        fake = RefusesOneParameter(['{"name": "Apollo", "year": 2023}'], refuses="temperature")
        monkeypatch.setattr(litellm_adapter, "_litellm", fake)
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(litellm_adapter, "log_llm_call", records.append)

        await adapter().structured(Prompt(system="s", user="u"), schema=Answer)

        attempt = records[0]["attempts"][0]
        assert attempt["parameters_refused"] == ["temperature"]
        assert "temperature" not in attempt["request_parameters"]

    async def test_an_unrelated_bad_request_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only sampling is given up. A refused schema or output cap changes the question."""
        fake = RefusesOneParameter(['{"name": "Apollo"}'], refuses="max_tokens")
        monkeypatch.setattr(litellm_adapter, "_litellm", fake)

        with pytest.raises(ModelRequestError):
            await adapter().structured(Prompt(system="s", user="u"), schema=Answer)

        assert len(fake.calls) == 1


class SpendsTheBudgetOnThinking(StubLiteLLM):
    """A reasoning model at default effort: the cap is gone and the reply is empty.

    Its thinking is billed against ``max_tokens`` and never streamed, so the stream ends
    with ``finish_reason: length`` and no content. Measured on gpt-5-nano: a placement
    choice came back ``out_tokens: 32``, ``finish_reason: length``, reply ``''`` -- twice,
    so the document failed -- and a card spent 2048 and then 4096 tokens the same way.
    """

    def __init__(self, replies: list[str], *, answers_above: int) -> None:
        super().__init__(replies)
        self._answers_above = answers_above

    async def acompletion(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        if int(kwargs.get("max_tokens", 0)) <= self._answers_above:
            return FakeStream("", finish_reason="length")
        return FakeStream(self._replies[min(len(self.calls) - 1, len(self._replies) - 1)])


class TestReasoningIsNotWhatWasAsked:
    @pytest.fixture(autouse=True)
    def _forget_between_tests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(litellm_adapter, "_UNSUPPORTED", {})

    async def test_a_classification_call_asks_for_minimal_reasoning(self, stub: Any) -> None:
        """There is nothing to reason about: the question is closed and the answers listed."""
        fake = stub(['{"name": "Apollo", "year": 2023}'])

        await adapter().structured(Prompt(system="s", user="u"), schema=Answer)

        assert fake.calls[0]["reasoning_effort"] == "minimal"

    async def test_a_configured_effort_is_not_overridden(self, stub: Any) -> None:
        """The operator knows their endpoint; Bismuth only supplies the default."""
        fake = stub(['{"name": "Apollo", "year": 2023}'])

        await adapter(body={"reasoning_effort": "high"}).structured(
            Prompt(system="s", user="u"), schema=Answer
        )

        assert fake.calls[0]["reasoning_effort"] == "high"

    async def test_an_endpoint_that_refuses_the_effort_is_obeyed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = RefusesOneParameter(['{"name": "Apollo", "year": 2023}'], refuses="reasoning_effort")
        monkeypatch.setattr(litellm_adapter, "_litellm", fake)

        result = await adapter().structured(Prompt(system="s", user="u"), schema=Answer)

        assert result == Answer(name="Apollo", year=2023)
        assert "reasoning_effort" not in fake.calls[1]

    async def test_a_choice_gets_room_for_thinking_it_could_not_avoid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """32 tokens is generous for a literal and nothing at all for a thinking model."""
        fake = SpendsTheBudgetOnThinking(["F003"], answers_above=32)
        monkeypatch.setattr(litellm_adapter, "_litellm", fake)

        chosen = await adapter().choose(Prompt(system="s", user="u"), choices=["F003", "STAY"])

        assert chosen == "F003"
        assert fake.calls[0]["max_tokens"] == 32
        assert fake.calls[1]["max_tokens"] == 256

    async def test_a_spent_budget_does_not_report_a_long_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Output reached the limit" about an empty reply sends the reader after a
        length problem that is not there. The cap was a total, not an output cap."""
        fake = SpendsTheBudgetOnThinking([""], answers_above=_MAX_CHOICE_MAX_TOKENS)
        monkeypatch.setattr(litellm_adapter, "_litellm", fake)

        with pytest.raises(StructuredOutputError, match="without producing any output"):
            await adapter().choose(Prompt(system="s", user="u"), choices=["F003", "STAY"])
