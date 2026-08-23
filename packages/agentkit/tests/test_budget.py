"""Context budgeting: clipping, compaction, eviction, and running out of budget."""

from __future__ import annotations

import itertools

import pytest
from pydantic import BaseModel

from agentkit import Agent, budget
from agentkit.messages import AssistantMessage, Message
from agentkit.testing import FakeModel, call, says
from agentkit.tool import FunctionTool


class TextArgs(BaseModel):
    text: str = ""


def big_tool(size: int) -> FunctionTool:
    async def _big(args: TextArgs) -> str:
        return "\n".join(f"line {i}" for i in range(size))

    return FunctionTool(name="big", description="Return a lot.", params=TextArgs, handler=_big)


def test_estimate_does_not_undercount_korean() -> None:
    korean = "법령의 시행일은 공포한 날부터 기산한다" * 20
    # A Hangul syllable is about one token; undercounting is what overflows a window.
    assert budget.estimate(korean) >= len(korean.replace(" ", ""))


def test_clip_keeps_both_ends_and_says_what_it_dropped() -> None:
    text = "\n".join(str(i) for i in range(1000))
    clipped = budget.clip(text, limit_chars=100, head=3, tail=2)
    assert clipped.startswith("0\n1\n2\n")
    assert clipped.endswith("998\n999")
    assert "995 lines omitted" in clipped


def test_clip_leaves_a_short_result_alone() -> None:
    assert budget.clip("short", limit_chars=100) == "short"


def test_microcompact_clears_oldest_and_keeps_recent() -> None:
    messages = [Message("tool", "x" * 3000, tool_call_id=str(i)) for i in range(4)]
    freed = budget.microcompact(messages, keep_recent=2)
    assert freed > 0
    assert [m.content == budget.CLEARED for m in messages] == [True, True, False, False]
    assert [m.tool_call_id for m in messages] == ["0", "1", "2", "3"]


def test_microcompact_stops_once_it_has_freed_enough() -> None:
    messages = [Message("tool", "x" * 3000, tool_call_id=str(i)) for i in range(4)]
    budget.microcompact(messages, keep_recent=1, need=10)
    assert [m.content == budget.CLEARED for m in messages] == [True, False, False, False]


def test_microcompact_never_clears_the_last_result() -> None:
    messages = [Message("tool", "x" * 3000, tool_call_id="1")]
    assert budget.microcompact(messages, keep_recent=0) == 0
    assert messages[0].content != budget.CLEARED


def test_microcompact_leaves_the_user_and_the_model_alone() -> None:
    messages = [
        Message("user", "question"),
        Message("assistant", "reasoning", (call("big"),)),
        Message("tool", "y" * 3000, tool_call_id="c1"),
        Message("tool", "z" * 3000, tool_call_id="c2"),
    ]
    budget.microcompact(messages, keep_recent=1)
    assert messages[0].content == "question"
    assert messages[1].content == "reasoning"


def test_evict_never_leaves_a_result_answering_a_dropped_call() -> None:
    messages = [
        Message("user", "old question"),
        Message("assistant", "", (call("big"),)),
        Message("tool", "r" * 3000, tool_call_id="c1"),
        Message("user", "new question"),
    ]
    budget.evict(messages, need=20)
    assert [m.role for m in messages] == ["user"]
    assert messages[0].content == "new question"


@pytest.mark.asyncio
async def test_transcript_is_compacted_instead_of_overflowing() -> None:
    """A run whose tool results outgrow the window keeps going on cleared results."""
    turns = [says("", call("big", {}, call_id=f"c{i}")) for i in range(12)] + [says("done")]
    model = FakeModel(turns)
    agent = Agent(
        model=model,
        tools=[big_tool(200)],
        system="s",
        context_tokens=8_000,
        result_max_chars=100_000,
    )
    result = await agent.run("go")

    assert result.stopped == "final"
    assert any(event.kind == "compact" for event in result.events)
    sent = model.calls[-1][1]
    assert any(m.content == budget.CLEARED for m in sent), "nothing was cleared"
    assert sum(1 for m in sent if m.role == "tool" and m.content != budget.CLEARED) >= 1
    assert budget.transcript_tokens("s", sent) < 8_000
    assert sent[0].content == "go", "the question itself must survive compaction"


@pytest.mark.asyncio
async def test_a_tool_result_is_clipped_before_it_enters_the_transcript() -> None:
    model = FakeModel([says("", call("big")), says("done")])
    agent = Agent(model=model, tools=[big_tool(5_000)], system="s", result_max_chars=500)
    await agent.run("go")

    result = next(m for m in model.calls[-1][1] if m.role == "tool")
    assert len(result.content) < 2_000
    assert "lines omitted" in result.content


@pytest.mark.asyncio
async def test_a_spent_run_still_answers() -> None:
    """Out of budget is not a reason to say nothing: the tools go away, the question stays."""
    seen: list[int] = []

    def handler(system: str, messages: list[Message], tools: list[object]) -> object:
        seen.append(len(tools))
        return says("찾은 것까지는 이렇다") if not tools else says("", call("big"))

    model = FakeModel(handler=handler)
    agent = Agent(
        model=model,
        tools=[big_tool(50)],
        system="s",
        budget_tokens=2_000,
        max_turns=100,
    )
    result = await agent.run("go")

    assert result.stopped == "budget"
    assert result.text == "찾은 것까지는 이렇다"
    assert seen[-1] == 0, "the last turn must be asked without tools"
    assert result.turns < 100, "the budget, not the backstop, ended this"


@pytest.mark.asyncio
async def test_the_turn_backstop_also_answers() -> None:
    def handler(system: str, messages: list[Message], tools: list[object]) -> object:
        return says("여기까지 확인했다") if not tools else says("", call("big"))

    agent = Agent(
        model=FakeModel(handler=handler),
        tools=[big_tool(1)],
        system="s",
        max_turns=3,
        budget_tokens=10_000_000,
    )
    result = await agent.run("go")

    assert result.stopped == "max_turns"
    assert result.text == "여기까지 확인했다"


@pytest.mark.asyncio
async def test_a_refused_request_is_retried_against_a_smaller_transcript() -> None:
    """The estimate can be wrong; the provider saying no is not the end of the run."""
    tries: list[int] = []

    def handler(system: str, messages: list[Message], tools: list[object]) -> object:
        tries.append(len(messages))
        if len(tries) == 2:
            raise RuntimeError("ContextWindowExceededError")
        return says("done") if len(tries) > 2 else says("", call("big", {}, call_id="c1"))

    model = FakeModel(handler=handler)
    agent = Agent(
        model=model,
        tools=[big_tool(3_000)],
        system="s",
        context_tokens=1_000_000,
        result_max_chars=100_000,
    )
    result = await agent.run("go")

    assert result.text == "done"
    assert any(e.kind == "compact" for e in result.events)


@pytest.mark.asyncio
async def test_history_goes_before_the_question_does() -> None:
    """Compaction eats an earlier conversation first; the question asked is untouchable."""
    history = [Message("user", "옛 질문 " * 2_000), Message("assistant", "옛 답 " * 2_000)]
    model = FakeModel([says("", call("big", {}, call_id="c1")), says("답")])
    agent = Agent(
        model=model,
        tools=[big_tool(50)],
        system="s",
        context_tokens=6_000,
        result_max_chars=100_000,
    )
    result = await agent.run("이번 질문", history=history)

    sent = model.calls[-1][1]
    assert any(m.content == "이번 질문" for m in sent), "the question was evicted"
    assert not any(m.content.startswith("옛 질문") for m in sent), "history should go first"
    assert result.text == "답"


def test_since_trusts_the_measured_prefix_over_the_estimate() -> None:
    """A measured count plus the delta -- not a guess at the whole thing."""
    messages = [Message("user", "x" * 100_000), Message("tool", "y" * 30, tool_call_id="1")]

    # The provider said the first message's request cost 7 tokens. It is wrong about
    # nothing: that IS what it cost. Only what came after it is estimated.
    counted = budget.since(7, messages, at=1)

    assert counted < 50, "the measured prefix was re-estimated instead of trusted"
    assert counted > 7, "what came after the measurement has to be counted too"


def test_fit_batch_takes_from_the_largest_first() -> None:
    small = "small\n" * 5
    huge = "\n".join(f"line {i}" for i in range(5_000))
    out = budget.fit_batch([small, huge, small], limit_chars=2_000)

    assert out[0] == small and out[2] == small, "the small results should survive whole"
    assert len(out[1]) < len(huge)
    assert sum(len(r) for r in out) <= 2_000


def test_fit_batch_leaves_a_turn_that_already_fits() -> None:
    results = ["a", "b", "c"]
    assert budget.fit_batch(results, limit_chars=1_000) == results


@pytest.mark.asyncio
async def test_the_loop_budgets_against_the_provider_count_when_it_has_one() -> None:
    """Several parallel results, each under the per-result cap, still bound as one turn."""
    turns = [
        AssistantMessage(
            "",
            tuple(call("big", {}, call_id=f"c{i}") for i in range(3)),
            input_tokens=9_000,
        ),
        says("done"),
    ]
    model = FakeModel(turns)
    agent = Agent(
        model=model,
        tools=[big_tool(2_000)],
        system="s",
        context_tokens=20_000,
        result_max_chars=100_000,
    )
    result = await agent.run("go")

    sent = [m for m in model.calls[-1][1] if m.role == "tool"]
    assert len(sent) == 3
    assert sum(len(m.content) for m in sent) <= 20_000 // 2, "the turn was not bounded"
    assert result.spent >= 9_000, "the provider's own count should be what is spent"


@pytest.mark.asyncio
async def test_compaction_leaves_room_instead_of_clearing_to_the_line() -> None:
    """Freeing exactly the overage puts the next turn over the line again, and every turn after.

    Measured as the gap between compactions: clearing to the ceiling makes it fire on
    consecutive turns once the transcript is full, which re-invalidates the prompt cache
    each time. Clearing to a target below it buys several quiet turns.
    """
    turns = [says("", call("big", {}, call_id=f"c{i}")) for i in range(30)] + [says("done")]
    agent = Agent(
        model=FakeModel(turns),
        tools=[big_tool(120)],
        system="s",
        context_tokens=6_000,
        result_max_chars=100_000,
    )
    result = await agent.run("go")

    at = [
        index
        for index, event in enumerate(e for e in result.events if e.kind in ("turn", "compact"))
        if event.kind == "compact"
    ]
    assert len(at) >= 2, "the transcript never filled up, so this proves nothing"
    gaps = [b - a for a, b in itertools.pairwise(at)]
    assert min(gaps) >= 3, f"compacted again after {min(gaps)} turn(s) -- it cleared to the line"


@pytest.mark.asyncio
async def test_a_stated_context_limit_is_adopted_over_the_configured_one() -> None:
    """The configured window is an assumption; the refusal that names one is a fact."""
    tries: list[int] = []

    def handler(system: str, messages: list[Message], tools: list[object]) -> object:
        tries.append(1)
        if len(tries) == 1:
            refusal = RuntimeError("maximum context length is 8000 tokens")
            refusal.context_limit = 8_000  # type: ignore[attr-defined]
            raise refusal
        return says("done")

    agent = Agent(
        model=FakeModel(handler=handler),
        tools=[big_tool(400)],
        system="s",
        context_tokens=500_000,
        result_max_chars=100_000,
    )
    result = await agent.run("go")

    assert result.text == "done"
    learned = [e for e in result.events if e.data.get("learned_from") == "provider"]
    assert learned and learned[0].data["context_tokens"] == 8_000
