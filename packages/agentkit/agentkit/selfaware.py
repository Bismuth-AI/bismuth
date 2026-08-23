"""Tools that let a run see its own state: what budget is left, and what it planned to do.

Both are taken from codex (`get_context_remaining`, `update_plan`). The reasoning behind
each is the same: an agent that cannot see a constraint cannot work within it.

Bismuth's measurements said the same thing twice. One question needed four laws, spent its
whole budget reading the first, and said so in its own answer -- "이 서고에 있는 단일 문서를
근거로 정리한 것입니다". Another opened every document it needed and then stopped at the turn
backstop. Neither knew how much was left, and neither had written down what it still owed.

Unlike the vault tools these are not about the corpus, so they live in agentkit: any agent
with a budget can use them, and the loop is the only thing that knows what the budget is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from agentkit.tool import FunctionTool

STATUSES = ("pending", "in_progress", "completed", "dropped")


@dataclass(slots=True)
class Spend:
    """What one run has spent, shared between the loop and the tool that reports it."""

    budget: int
    spent: int = 0

    @property
    def left(self) -> int:
        return max(0, self.budget - self.spent)

    @property
    def share_left(self) -> float:
        return self.left / self.budget if self.budget else 1.0


@dataclass(slots=True)
class Step:
    step: str
    status: str = "pending"


@dataclass(slots=True)
class Plan:
    """What the agent said it would do, and how far along it is."""

    steps: list[Step] = field(default_factory=list)

    def render(self) -> str:
        if not self.steps:
            return "(계획 없음)"
        mark = {"pending": "☐", "in_progress": "▶", "completed": "☑", "dropped": "✕"}
        return "\n".join(f"{mark[s.status]} {s.step}" for s in self.steps)

    def unfinished(self) -> list[str]:
        return [s.step for s in self.steps if s.status in ("pending", "in_progress")]


class _NoArgs(BaseModel):
    pass


class _StepIn(BaseModel):
    step: str = Field(description="무엇을 할지 한 줄로.")
    status: str = Field(
        default="pending",
        description="pending | in_progress | completed | dropped. 동시에 in_progress 는 하나만.",
    )


class _PlanIn(BaseModel):
    plan: list[_StepIn] = Field(description="계획 전체. 부분이 아니라 매번 전체를 준다.")
    note: str = Field(default="", description="계획을 바꿨다면 왜 바꿨는지 한 줄.")


def budget_tool(spend: Spend) -> FunctionTool:
    """Lets the agent ask how much room is left before it has to answer."""

    async def _left(args: _NoArgs) -> str:
        if not spend.budget:
            return "예산 한도가 없다."
        return (
            f"예산의 {spend.share_left:.0%}가 남았다 (약 {spend.left:,} 토큰). "
            f"다 쓰면 도구 없이 답만 쓰게 된다."
        )

    return FunctionTool(
        name="budget",
        description=(
            "How much of this question's budget is left. Ask before starting anything "
            "long, and when deciding whether to look at one more thing."
        ),
        params=_NoArgs,
        handler=_left,
    )


def plan_tool(plan: Plan) -> FunctionTool:
    """Lets the agent write down what it owes, so a wide question is not silently narrowed."""

    async def _update(args: _PlanIn) -> str:
        for item in args.plan:
            if item.status not in STATUSES:
                return f"status 는 {' | '.join(STATUSES)} 중 하나여야 한다: {item.status!r}"
        running = [i for i in args.plan if i.status == "in_progress"]
        if len(running) > 1:
            return (
                "in_progress 는 한 번에 하나만 둔다. 지금 하는 것 하나만 in_progress 로 두고 "
                f"나머지는 pending 으로: {[i.step for i in running]}"
            )
        plan.steps = [Step(step=i.step, status=i.status) for i in args.plan]
        return plan.render()

    return FunctionTool(
        name="plan",
        description=(
            "Write down the parts of the question and track them. Use it when the question "
            "has more than one part -- 'both', 'each', 'compare', 'A and B' -- so that "
            "finishing one part does not end the search. Not for a question with one part."
        ),
        params=_PlanIn,
        handler=_update,
    )
