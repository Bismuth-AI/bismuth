"""The two tools a run uses to see itself: what budget is left, and what it still owes."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agentkit import Agent
from agentkit.messages import Message
from agentkit.selfaware import Plan, Spend, budget_tool, plan_tool
from agentkit.testing import FakeModel, call, says
from agentkit.tool import FunctionTool


class Nothing(BaseModel):
    pass


def noop() -> FunctionTool:
    async def _run(args: Nothing) -> str:
        return "ok"

    return FunctionTool(name="look", description="Look at something.", params=Nothing, handler=_run)


async def run_tool(tool: FunctionTool, **kwargs: object) -> str:
    return await tool.run(tool.params(**kwargs))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_budget_reports_what_is_left() -> None:
    spend = Spend(budget=1000, spent=750)
    assert "25%" in await run_tool(budget_tool(spend))


@pytest.mark.asyncio
async def test_plan_refuses_two_things_at_once() -> None:
    plan = Plan()
    reply = await run_tool(
        plan_tool(plan),
        plan=[{"step": "A", "status": "in_progress"}, {"step": "B", "status": "in_progress"}],
    )
    assert "하나만" in reply
    assert not plan.steps, "a refused update must not be applied"


@pytest.mark.asyncio
async def test_plan_keeps_what_was_written() -> None:
    plan = Plan()
    await run_tool(
        plan_tool(plan),
        plan=[
            {"step": "전자상거래법", "status": "completed"},
            {"step": "방문판매법", "status": "in_progress"},
            {"step": "표시광고법", "status": "pending"},
        ],
    )
    assert plan.unfinished() == ["방문판매법", "표시광고법"]


@pytest.mark.asyncio
async def test_the_warning_comes_once_and_early_enough_to_act_on() -> None:
    """A warning at the moment the budget runs out is not information."""
    seen: list[str] = []

    def handler(system: str, messages: list[Message], tools: list[object]) -> object:
        seen.extend(m.content for m in messages if m.role == "user")
        return says("", call("look"))

    agent = Agent(
        model=FakeModel(handler=handler),
        tools=[noop()],
        system="s",
        budget_tokens=3_000,
        low_budget="남은 예산 {share:.0%}",
        max_turns=40,
    )
    result = await agent.run("go")

    warnings = [e for e in result.events if e.kind == "budget_low"]
    assert len(warnings) == 1, f"the warning must be latched, not repeated: {len(warnings)}"
    assert warnings[0].data["share"] <= 0.3
    turn_of_warning = next(i for i, e in enumerate(result.events) if e.kind == "budget_low")
    stop = next(i for i, e in enumerate(result.events) if e.kind == "stop")
    assert stop - turn_of_warning > 3, "the warning arrived with no room left to use it"


@pytest.mark.asyncio
async def test_a_spent_run_is_told_what_it_left_undone() -> None:
    def handler(system: str, messages: list[Message], tools: list[object]) -> object:
        if not tools:
            return says("여기까지입니다")
        if not any(m.role == "tool" for m in messages):
            return says(
                "",
                call(
                    "plan",
                    {
                        "plan": [
                            {"step": "전자상거래법", "status": "in_progress"},
                            {"step": "방문판매법", "status": "pending"},
                        ]
                    },
                ),
            )
        return says("", call("look"))

    agent = Agent(
        model=FakeModel(handler=handler),
        tools=[noop()],
        system="s",
        budget_tokens=3_000,
        self_tools=("plan", "budget"),
        max_turns=40,
    )
    result = await agent.run("두 법을 비교하라")

    final = [m for m in result.messages if m.role == "user"][-1]
    assert "아직 못 끝낸 것" in final.content
    assert "방문판매법" in final.content


@pytest.mark.asyncio
async def test_the_extra_tools_are_off_unless_asked_for() -> None:
    model = FakeModel([says("done")])
    await Agent(model=model, tools=[noop()], system="s").run("go")

    names = {spec.name for spec in model.calls[-1][2]}
    assert names == {"look"}
