"""Tools for tracking a run's budget and plan."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from bismuth.agentkit.tool import FunctionTool

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
            return "(no plan)"
        mark = {"pending": "☐", "in_progress": "▶", "completed": "☑", "dropped": "✕"}
        return "\n".join(f"{mark[s.status]} {s.step}" for s in self.steps)

    def unfinished(self) -> list[str]:
        return [s.step for s in self.steps if s.status in ("pending", "in_progress")]


class _NoArgs(BaseModel):
    pass


class _StepIn(BaseModel):
    step: str = Field(description="One concise action.")
    status: str = Field(
        default="pending",
        description="pending | in_progress | completed | dropped. Only one may be in progress.",
    )


class _PlanIn(BaseModel):
    plan: list[_StepIn] = Field(description="The complete plan, including unchanged steps.")
    note: str = Field(default="", description="A concise reason for changing the plan.")


def budget_tool(spend: Spend) -> FunctionTool:
    """Create a tool that reports the remaining run budget."""

    async def _left(args: _NoArgs) -> str:
        if not spend.budget:
            return "No budget limit is set."
        return (
            f"{spend.share_left:.0%} of the budget remains (about {spend.left:,} tokens). "
            "When it is exhausted, no more tools can be used."
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
    """Create a tool that replaces and reports the current plan."""

    async def _update(args: _PlanIn) -> str:
        for item in args.plan:
            if item.status not in STATUSES:
                return f"Status must be one of {' | '.join(STATUSES)}: {item.status!r}"
        running = [i for i in args.plan if i.status == "in_progress"]
        if len(running) > 1:
            return (
                "Only one step may be in progress. Mark the others as pending: "
                f"{[i.step for i in running]}"
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
