"""The agent loop: call the model, run the tools it asks for, feed results back, repeat."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from bismuth.agentkit import budget
from bismuth.agentkit.messages import AssistantMessage, Message, ToolCall, ToolSpec
from bismuth.agentkit.model import ChatModel, ContextWindowExceededError
from bismuth.agentkit.registry import ToolRegistry
from bismuth.agentkit.selfaware import Plan, Spend, budget_tool, plan_tool
from bismuth.agentkit.tool import Permission, Tool

OnAsk = Callable[[Tool, BaseModel], Awaitable[Permission]]
OnEvent = Callable[["AgentEvent"], None]
OnText = Callable[[str], None]

CONTEXT_TOKENS = 32_000
"""What the model's window is assumed to be when the caller does not say."""

RESERVE_TOKENS = 3_000
"""Held back from the window for the turn the model is about to write."""

SPEND_MULTIPLE = 12
"""Maximum run budget as a multiple of the context window."""

TURN_SHARE = 2
"""Maximum combined tool-result size as a fraction of the window."""

RESULT_SHARE = 4
"""Maximum single tool-result size as a fraction of the window."""

COMPACT_TARGET = 0.6
"""Target transcript size after compaction, as a share of the ceiling."""

KEEP_RECENT_RESULTS = 4
"""Recent tool results preserved during compaction."""

LOW_BUDGET_SHARE = 0.15
"""Remaining budget share that triggers one warning."""

LOW_BUDGET = (
    "About {share:.0%} of the budget for this question is left. Decide what still has "
    "to be looked at and do that first; anything still unfinished when the budget runs "
    "out will have to be reported as unfinished."
)

OUT_OF_BUDGET = (
    "You are out of budget for further tool calls. Answer now, from what you have "
    "already found. If it is not enough for a full answer, say what you did establish "
    "and what is still missing -- do not stop without answering."
)


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One step, for logging or debugging a run.

    kind is one of: ``turn``, ``tool_call``, ``tool_result``, ``tool_denied``,
    ``tool_error``, ``compact``, ``stop``.
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
    stopped: str  # "final" (model finished) | "budget" (spent up) | "max_turns" (backstop)
    spent: int = 0


class Agent:
    """Run a model with tools, permissions, context limits, and a token budget."""

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: Iterable[Tool] | ToolRegistry,
        system: str,
        max_turns: int = 60,
        context_tokens: int = CONTEXT_TOKENS,
        budget_tokens: int | None = None,
        result_max_chars: int | None = None,
        out_of_budget: str = OUT_OF_BUDGET,
        low_budget: str = "",
        self_tools: tuple[str, ...] = (),
        on_ask: OnAsk | None = None,
        on_event: OnEvent | None = None,
    ) -> None:
        self._model = model
        self._registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        self._system = system
        self._max_turns = max_turns
        self._context_tokens = context_tokens
        self._budget_tokens = (
            budget_tokens if budget_tokens is not None else context_tokens * SPEND_MULTIPLE
        )
        self._result_max_chars = (
            result_max_chars if result_max_chars is not None else context_tokens // RESULT_SHARE
        )
        self._turn_max_chars = context_tokens // TURN_SHARE
        self._anchor: tuple[int, int] | None = None
        self._out_of_budget = out_of_budget
        self._low_budget = low_budget
        self._self_tools = self_tools
        self._on_ask = on_ask
        self._on_event = on_event

    async def run(
        self,
        user_input: str,
        *,
        history: Iterable[Message] = (),
        on_event: OnEvent | None = None,
        on_text: OnText | None = None,
    ) -> RunResult:
        """Run until completion and return the answer with its transcript and events."""
        messages: list[Message] = [*history, Message("user", user_input)]
        events: list[AgentEvent] = []
        sink = on_event if on_event is not None else self._on_event
        asked_at = len(messages) - 1  # everything from here on is this run's own work
        self._anchor = None

        # Keep budget and plan state isolated to this run.
        spend = Spend(budget=self._budget_tokens)
        plan = Plan()
        extra = []
        if "plan" in self._self_tools:
            extra.append(plan_tool(plan))
        if "budget" in self._self_tools:
            extra.append(budget_tool(spend))
        registry = ToolRegistry([*self._registry.all(), *extra]) if extra else self._registry
        specs = registry.specs()
        warned = False

        def emit(kind: str, **data: object) -> None:
            event = AgentEvent(kind, data)
            events.append(event)
            if sink is not None:
                sink(event)

        last_text = ""
        turn = 0
        for turn in range(1, self._max_turns + 1):
            cost = self._fit(messages, specs, emit, pinned=len(messages) - asked_at)
            sent = len(messages)
            reply = await self._complete(messages, specs, emit, on_text=on_text)
            if reply.input_tokens:
                # Prefer the provider's measured token count.
                self._anchor, cost = (sent, reply.input_tokens), reply.input_tokens
            spend.spent += cost + budget.estimate(reply.text)
            last_text = reply.text
            messages.append(Message("assistant", reply.text, reply.tool_calls))
            emit("turn", index=turn, text=reply.text, tools=[c.name for c in reply.tool_calls])

            if not reply.tool_calls:
                emit("stop", reason="final")
                return RunResult(reply.text, messages, events, turn, "final", spend.spent)

            for call in reply.tool_calls:
                emit("tool_call", id=call.id, name=call.name, arguments=call.arguments)
            done = await self._dispatch_calls(reply.tool_calls, registry)
            fitted = budget.fit_batch(
                [content for content, _ in done], limit_chars=self._turn_max_chars
            )
            for call, content, (_, kind) in zip(reply.tool_calls, fitted, done, strict=True):
                messages.append(Message("tool", content, tool_call_id=call.id))
                # Keep event previews bounded; the transcript retains the full result.
                emit(kind, id=call.id, name=call.name, preview=content[:1200])

            if spend.left <= 0:
                emit("stop", reason="budget", spent=spend.spent)
                text = await self._answer_anyway(
                    messages,
                    emit,
                    on_text=on_text,
                    pinned=len(messages) - asked_at,
                    owed=plan.unfinished(),
                )
                return RunResult(text or last_text, messages, events, turn, "budget", spend.spent)

            if self._low_budget and not warned and spend.share_left <= LOW_BUDGET_SHARE:
                # Warn once while there is still budget to act.
                warned = True
                messages.append(Message("user", self._low_budget.format(share=spend.share_left)))
                emit("budget_low", share=spend.share_left, left=spend.left)

        emit("stop", reason="max_turns")
        text = await self._answer_anyway(
            messages,
            emit,
            on_text=on_text,
            pinned=len(messages) - asked_at,
            owed=plan.unfinished(),
        )
        return RunResult(text or last_text, messages, events, turn, "max_turns", spend.spent)

    def _fit(
        self,
        messages: list[Message],
        specs: Sequence[ToolSpec],
        emit: Callable[..., None],
        *,
        pinned: int,
    ) -> int:
        """Fit the transcript under the context ceiling and return its token cost."""
        ceiling = self._context_tokens - RESERVE_TOKENS
        used = (
            budget.since(self._anchor[1], messages, self._anchor[0])
            if self._anchor is not None
            else budget.transcript_tokens(self._system, messages, specs)
        )
        if used <= ceiling:
            return used
        over = used - ceiling
        # Clear stale results before evicting conversation history.
        freed = budget.microcompact(
            messages,
            keep_recent=KEEP_RECENT_RESULTS,
            need=used - int(ceiling * COMPACT_TARGET),
        )
        if freed < over:
            freed += budget.evict(messages, need=over - freed, keep_last=pinned)
        if freed < over:
            freed += budget.microcompact(messages, keep_recent=1, need=over - freed)
        emit("compact", before=used, after=used - freed, freed=freed)
        self._anchor = None
        return used - freed

    async def _complete(
        self,
        messages: list[Message],
        specs: Sequence[ToolSpec],
        emit: Callable[..., None],
        *,
        on_text: OnText | None,
        tools: bool = True,
    ) -> AssistantMessage:
        """Call the model and retry one context overflow after compaction."""
        try:
            return await self._model.complete(
                system=self._system,
                messages=messages,
                tools=specs if tools else (),
                on_text=on_text,
            )
        except ContextWindowExceededError as exc:
            stated = exc.context_limit
            learned = bool(stated) and stated < self._context_tokens
            if learned:
                self._context_tokens = stated
                self._result_max_chars = stated // RESULT_SHARE
                self._turn_max_chars = stated // TURN_SHARE
                self._anchor = None
                emit("compact", context_tokens=stated, learned_from="provider")
            freed = budget.microcompact(messages, keep_recent=1)
            freed += budget.shrink(messages, limit_chars=self._result_max_chars // 8)
            if not freed and not learned:
                raise
            emit("compact", freed=freed, after_error=type(exc).__name__)
            return await self._model.complete(
                system=self._system,
                messages=messages,
                tools=specs if tools else (),
                on_text=on_text,
            )

    async def _answer_anyway(
        self,
        messages: list[Message],
        emit: Callable[..., None],
        *,
        on_text: OnText | None,
        pinned: int,
        owed: list[str] | None = None,
    ) -> str:
        """Request a final answer without tools."""
        nudge = self._out_of_budget
        if owed:
            nudge += "\n\nUnfinished items: " + " / ".join(owed)
        messages.append(Message("user", nudge))
        self._fit(messages, (), emit, pinned=pinned + 1)
        try:
            reply = await self._complete(messages, (), emit, on_text=on_text, tools=False)
        except Exception as exc:
            emit("tool_error", name="final", preview=f"{type(exc).__name__}: {exc}")
            return ""
        messages.append(Message("assistant", reply.text))
        emit("turn", index=0, text=reply.text, tools=[], final=True)
        return reply.text

    async def _dispatch_calls(
        self, calls: tuple[ToolCall, ...], registry: ToolRegistry
    ) -> list[tuple[str, str]]:
        """Run a turn's tool calls -- in parallel when every one is concurrency-safe."""
        safe = all(getattr(registry.get(c.name), "concurrency_safe", False) for c in calls)
        if safe and len(calls) > 1:
            return list(await asyncio.gather(*(self._dispatch(c, registry) for c in calls)))
        return [await self._dispatch(c, registry) for c in calls]

    async def _dispatch(self, call: ToolCall, registry: ToolRegistry) -> tuple[str, str]:
        """Run one tool call through parse -> permission -> execute. Returns (content, event_kind)."""
        tool = registry.get(call.name)
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
            result = await tool.run(args)
        except Exception as exc:
            return f"Error running '{call.name}': {exc}", "tool_error"
        return budget.clip(result, limit_chars=self._result_max_chars), "tool_result"
