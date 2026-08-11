"""LiteLLMAdapter logic against a stubbed litellm: fallback ladder, JSON salvage, repair, explicit key."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from bismuth.adapters.llm import litellm_adapter
from bismuth.adapters.llm.litellm_adapter import LiteLLMAdapter, _parse_json
from bismuth.domain.errors import ModelRequestError, StructuredOutputError
from bismuth.ports.llm import Prompt


class Answer(BaseModel):
    name: str
    year: int


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
    def __init__(self, reply: str) -> None:
        middle = max(1, len(reply) // 2)
        self._chunks = iter(
            [FakeChunk(reply[:middle]), FakeChunk(reply[middle:]), FakeChunk(finish_reason="stop")]
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

    async def test_giving_up_says_what_to_do(self, stub: Any) -> None:
        stub(["not json at all"])
        with pytest.raises(StructuredOutputError, match="choose a more capable model"):
            await adapter(max_schema_retries=0).structured(
                Prompt(system="s", user="u"), schema=Answer
            )


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
