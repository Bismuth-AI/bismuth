"""The agent loop: call the model, run the tools it asks for, feed results back, repeat."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from agentkit.context import ActiveContext, ContextPolicy, tool_call_key
from agentkit.messages import Message, ToolCall
from agentkit.model import ChatModel
from agentkit.registry import ToolRegistry
from agentkit.tool import Permission, Tool

OnAsk = Callable[[Tool, BaseModel], Awaitable[Permission]]
OnEvent = Callable[["AgentEvent"], None]
ConclusionAccepted = Callable[[ToolCall, str, str], bool]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One step, for logging or debugging a run.

    kind is one of: ``turn``, ``tool_call``, ``tool_result``, ``tool_denied``,
    ``tool_error``, ``stop``.
    """

    kind: str
    data: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class RunResult:
    """The outcome of a run: the final answer plus the full trace."""

    text: str
    messages: list[Message]
    events: list[AgentEvent]
    turns: int
    stopped: str  # "final" | "conclusion" | "max_turns" | "stalled"


class Agent:
    """A tool-using agent over one ChatModel and a fixed tool set.

    The loop is: ask the model for a turn; if it requested tools, run each through
    the permission gate and feed the results back; stop when a turn has no tool
    calls. Mutating tools return ``Permission.ASK`` and only run if ``on_ask``
    approves -- otherwise the model is told to propose the action, not take it.
    """

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: Iterable[Tool] | ToolRegistry,
        system: str,
        max_turns: int | None = 24,
        conclusion_turns: int = 0,
        conclusion_tools: Iterable[str] = (),
        conclusion_accepted: ConclusionAccepted | None = None,
        require_conclusion_tool: bool = True,
        tool_choice: str | None = None,
        context_policy: ContextPolicy | None = None,
        on_ask: OnAsk | None = None,
        on_event: OnEvent | None = None,
    ) -> None:
        self._model = model
        self._registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        self._system = system
        self._max_turns = max_turns
        # Retained as a source-compatible constructor argument. Tool schemas remain
        # stable for the lifetime of one transcript; callers that need an isolated
        # conclusion create a fresh Agent containing only conclusion tools.
        self._conclusion_turns = conclusion_turns
        self._conclusion_tools = set(conclusion_tools)
        self._conclusion_accepted = conclusion_accepted
        self._require_conclusion_tool = require_conclusion_tool
        self._tool_choice = tool_choice
        self._context_policy = context_policy
        self._context = ActiveContext(context_policy) if context_policy is not None else None
        if self._context is not None:
            self._registry.add(self._context.recall_tool())
        self._on_ask = on_ask
        self._on_event = on_event

    async def run(self, user_input: str, *, on_event: OnEvent | None = None) -> RunResult:
        """Run to completion. ``on_event`` overrides the instance sink for this run."""
        messages: list[Message] = [Message("user", user_input)]
        active_messages = list(messages)
        events: list[AgentEvent] = []
        sink = on_event if on_event is not None else self._on_event

        def emit(kind: str, **data: object) -> None:
            event = AgentEvent(kind, data)
            events.append(event)
            if sink is not None:
                sink(event)

        last_text = ""
        turn = 0
        repeated_warning = False
        conclusion_reached = False
        call_counts: dict[str, int] = {}
        context = self._context
        if context is not None:
            context.reset()

        while self._max_turns is None or turn < self._max_turns:
            turn += 1
            allowed = None
            specs = self._registry.specs()
            active_system = self._system
            if context is not None:
                compacted = context.compact(
                    active_messages,
                    system=active_system,
                    tools=specs,
                )
                if compacted.archived_results:
                    emit(
                        "context_compacted",
                        archived_results=compacted.archived_results,
                        tokens_before=compacted.tokens_before,
                        tokens_after=compacted.tokens_after,
                    )
                if compacted.still_over_limit:
                    active_system += (
                        "\n\n<agentkit-budget>The active context remains near its limit. "
                        "Conclude from collected evidence now.</agentkit-budget>"
                    )
            reply = await self._model.complete(
                system=active_system,
                messages=active_messages,
                tools=specs,
                tool_choice=self._tool_choice,
            )
            last_text = reply.text
            messages.append(Message("assistant", reply.text, reply.tool_calls))
            active_messages.append(Message("assistant", reply.text, reply.tool_calls))
            emit(
                "turn",
                index=turn,
                turn=turn,
                call_id=reply.call_id,
                text=reply.text,
                tools=[c.name for c in reply.tool_calls],
            )

            if (
                not reply.tool_calls
                and self._conclusion_tools
                and self._require_conclusion_tool
                and not conclusion_reached
            ):
                available = ", ".join(sorted(self._conclusion_tools))
                reminder = (
                    "Your prose did not complete this task. Call exactly one required "
                    f"conclusion tool now: {available}. Do not restate the analysis."
                )
                messages.append(Message("user", reminder))
                active_messages.append(Message("user", reminder))
                emit("conclusion_required", tools=sorted(self._conclusion_tools))
                continue

            if not reply.tool_calls:
                emit("stop", reason="final")
                return RunResult(reply.text, messages, events, turn, "final")

            repeated: set[str] = set()
            for call in reply.tool_calls:
                emit(
                    "tool_call",
                    turn=turn,
                    llm_call_id=reply.call_id,
                    id=call.id,
                    name=call.name,
                    arguments=call.arguments,
                )
                key = tool_call_key(call)
                call_counts[key] = call_counts.get(key, 0) + 1
                if (
                    self._context_policy is not None
                    and call_counts[key] > self._context_policy.repeated_call_limit
                ):
                    repeated.add(call.id)
            dispatched = await self._dispatch_calls(
                reply.tool_calls,
                repeated=repeated,
                allowed=allowed,
            )
            conclusion_results = [
                (call, content, kind)
                for call, (content, kind) in zip(reply.tool_calls, dispatched, strict=True)
                if call.name in self._conclusion_tools and kind == "tool_result"
            ]
            accepted_conclusion = bool(conclusion_results) and (
                self._conclusion_accepted is None
                or any(
                    self._conclusion_accepted(call, content, kind)
                    for call, content, kind in conclusion_results
                )
            )
            if accepted_conclusion:
                conclusion_reached = True
            for call, (content, kind) in zip(reply.tool_calls, dispatched, strict=True):
                messages.append(Message("tool", content, tool_call_id=call.id))
                projected = (
                    context.project_result(call.name, content) if context is not None else content
                )
                active_messages.append(Message("tool", projected, tool_call_id=call.id))
                emit(
                    kind,
                    turn=turn,
                    llm_call_id=reply.call_id,
                    id=call.id,
                    name=call.name,
                    content=content,
                    preview=content[:200],
                    chars=len(content),
                )

            if accepted_conclusion and self._conclusion_accepted is not None:
                emit("stop", reason="conclusion")
                return RunResult(last_text, messages, events, turn, "conclusion")

            if repeated:
                if repeated_warning:
                    emit("stop", reason="stalled")
                    return RunResult(last_text, messages, events, turn, "stalled")
                repeated_warning = True

        emit("stop", reason="max_turns")
        return RunResult(last_text, messages, events, turn, "max_turns")

    async def _dispatch_calls(
        self,
        calls: tuple[ToolCall, ...],
        *,
        repeated: set[str],
        allowed: set[str] | None,
    ) -> list[tuple[str, str]]:
        """Run a turn's tool calls -- in parallel when every one is concurrency-safe."""
        unavailable = {
            call.id for call in calls if allowed is not None and call.name not in allowed
        }
        if repeated or unavailable:
            results: list[tuple[str, str]] = []
            for call in calls:
                if call.id in repeated:
                    results.append(
                        (
                            "Repeated identical tool call blocked. Use the evidence already "
                            "collected, change the query, or conclude the task now.",
                            "tool_repeated",
                        )
                    )
                elif call.id in unavailable:
                    results.append(
                        (
                            f"Tool '{call.name}' is unavailable during the conclusion phase. "
                            "Use one of the currently advertised tools and finish now.",
                            "tool_unavailable",
                        )
                    )
                else:
                    results.append(await self._dispatch(call))
            return results
        safe = all(getattr(self._registry.get(c.name), "concurrency_safe", False) for c in calls)
        if safe and len(calls) > 1:
            return list(await asyncio.gather(*(self._dispatch(c) for c in calls)))
        return [await self._dispatch(c) for c in calls]

    async def _dispatch(self, call: ToolCall) -> tuple[str, str]:
        """Run one tool call through parse -> permission -> execute. Returns (content, event_kind)."""
        tool = self._registry.get(call.name)
        if tool is None:
            return f"Error: no tool named '{call.name}'.", "tool_error"

        try:
            args = tool.params.model_validate(call.arguments)
        except ValidationError as exc:
            return f"Error: invalid arguments for '{call.name}': {exc}", "tool_error"

        decision = tool.permission(args)
        if decision is Permission.ASK:
            decision = (
                await self._on_ask(tool, args) if self._on_ask is not None else Permission.DENY
            )
        if decision is not Permission.ALLOW:
            return (
                f"Not allowed: '{call.name}' needs user approval. "
                f"Propose it to the user instead of calling it.",
                "tool_denied",
            )

        try:
            return await tool.run(args), "tool_result"
        except Exception as exc:  # a tool failing must not crash the loop
            return f"Error running '{call.name}': {exc}", "tool_error"
