"""Isolated delegation to named sub-agents."""

from __future__ import annotations

import contextvars
from collections.abc import Mapping

from pydantic import BaseModel, Field

from bismuth.agentkit.loop import Agent, AgentEvent, OnEvent
from bismuth.agentkit.tool import FunctionTool, Tool

_DEFAULT_DESCRIPTION = """\
Delegate a self-contained task to a specialist sub-agent. Give it a full, \
standalone description -- it does not see this conversation, only what you write \
here. You get back its final answer. Available sub-agents: {agents}.\
"""

# Context-local depth prevents unbounded nested delegation.
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("agentkit_task_depth", default=0)


def subagent_tool(
    subagents: Mapping[str, Agent],
    *,
    name: str = "task",
    description: str | None = None,
    max_depth: int = 4,
    on_event: OnEvent | None = None,
) -> Tool:
    """Create a tool that delegates to a named sub-agent."""
    available = ", ".join(subagents) or "(none)"

    class TaskArgs(BaseModel):
        description: str = Field(description="Full standalone instructions for the sub-agent.")
        subagent_type: str = Field(description=f"Which sub-agent to run. One of: {available}.")

    async def handler(args: TaskArgs) -> str:
        agent = subagents.get(args.subagent_type)
        if agent is None:
            return f"No sub-agent '{args.subagent_type}'. Available: {available}."
        if _depth.get() >= max_depth:
            return f"Delegation depth limit ({max_depth}) reached; complete the task yourself."

        forward: OnEvent | None = None
        if on_event is not None:

            def forward(event: AgentEvent) -> None:
                on_event(
                    AgentEvent(f"sub:{event.kind}", {"subagent": args.subagent_type, **event.data})
                )

        token = _depth.set(_depth.get() + 1)
        try:
            result = await agent.run(args.description, on_event=forward)
        finally:
            _depth.reset(token)
        return result.text

    return FunctionTool(
        name=name,
        description=description or _DEFAULT_DESCRIPTION.format(agents=available),
        params=TaskArgs,
        handler=handler,
        read_only=True,
        concurrency_safe=False,  # Sub-agents may mutate shared state.
    )
