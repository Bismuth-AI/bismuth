"""The agent loop: call the model, run the tools it asks for, feed results back, repeat."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from agentkit import budget
from agentkit.messages import AssistantMessage, Message, ToolCall, ToolSpec
from agentkit.model import ChatModel
from agentkit.registry import ToolRegistry
from agentkit.selfaware import Plan, Spend, budget_tool, plan_tool
from agentkit.tool import Permission, Tool

OnAsk = Callable[[Tool, BaseModel], Awaitable[Permission]]
OnEvent = Callable[["AgentEvent"], None]
OnText = Callable[[str], None]

CONTEXT_TOKENS = 32_000
"""What the model's window is assumed to be when the caller does not say."""

RESERVE_TOKENS = 3_000
"""Held back from the window for the turn the model is about to write."""

SPEND_MULTIPLE = 12
"""How many windows' worth of tokens one run may spend before it must answer.

The budget is a multiple of the window rather than a count of turns, so a question
that is answered from small reads gets many looks and one that drags whole documents
in gets fewer -- which is the right way round.
"""

TURN_SHARE = 2
"""One turn's tool results together may occupy about half the window, in characters.

A per-result cap does not bound a turn. Several tools run in parallel here, and
each one just under the cap still arrives as a single message."""

RESULT_SHARE = 4
"""One tool result may occupy about a fifth of the window, counted in characters.

Derived from the window rather than fixed, because the two numbers only mean
anything together: a cap that is generous against a 200k window lets four results
fill a 32k one on their own, and then there is nothing left to compact."""

COMPACT_TARGET = 0.6
"""Where compaction aims, as a share of the ceiling -- not just under it.

Freeing exactly the overage means the next turn is over again, and every clearing
invalidates the prompt cache from that point on. The reference clears to a target
far below its trigger for the same reason (``clear_at_least`` in
``apiMicrocompact.ts``: fire at 180k, clear at least 140k)."""

KEEP_RECENT_RESULTS = 4
"""Tool results kept whole when compacting. Never fewer than one: an agent whose every
result has been cleared has no working context and begins its search again."""

LOW_BUDGET_SHARE = 0.15
"""When the warning goes out, as a share of the budget still unspent.

Once, and not again: a warning repeated every turn becomes part of the wallpaper. The
reference latches it the same way (``claim_token_budget_reminder``).

Late rather than early. At 0.3 the warning cost more than it saved -- questions that had
been answered fully began wrapping up with a third of their budget unspent, six of them
losing marks while finishing on their own rather than being cut off. The warning is for
a run that is about to be truncated, not for one that still has room to work."""

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
    """A tool-using agent over one ChatModel and a fixed tool set.

    The loop is: ask the model for a turn; if it requested tools, run each through
    the permission gate and feed the results back; stop when a turn has no tool
    calls. Mutating tools return ``Permission.ASK`` and only run if ``on_ask``
    approves -- otherwise the model is told to propose the action, not take it.

    What ends a run that will not end itself is the context window, not a turn count:
    the transcript is measured before every call and compacted when it nears the
    ceiling (see ``budget``), and the run stops once it has spent its token budget --
    with one last turn, tools withdrawn, so that it answers instead of falling silent.
    ``max_turns`` remains only as a backstop against a model that loops forever.
    """

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
        # Two knobs, not one: the tools the agent may call, and whether it is warned
        # unprompted. They were measured together and could not be told apart.
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
        """Run to completion. ``on_event`` overrides the instance sink for this run.

        ``history`` is what was said before, replayed ahead of this input so a second
        question can lean on the first: "그중 최신 것은?" is not a question anybody can
        answer from the words alone. The returned ``messages`` include it, so the caller
        can hand back what it got and let the transcript grow.
        """
        messages: list[Message] = [*history, Message("user", user_input)]
        events: list[AgentEvent] = []
        sink = on_event if on_event is not None else self._on_event
        asked_at = len(messages) - 1  # everything from here on is this run's own work
        self._anchor = None

        # Per run, because both are about this question: what is left of its budget and
        # what it still owes. A registry is built fresh so two runs never share a plan.
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
                # What the provider counted beats what we guessed, and anchoring here
                # keeps the guess to one turn instead of compounding over the run.
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
                # Long enough for a caller to see which documents a search actually
                # landed on, not just that it ran. The transcript keeps the whole thing;
                # this is the copy that goes to a log or a screen.
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
                # Said once, while there is still room to act on it. Telling an agent it
                # is out of time at the moment it runs out is not information, it is a
                # eulogy.
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
        """Compact the transcript back under the ceiling. Returns what the call will cost.

        ``pinned`` is how many messages at the end -- this run's question and everything
        since -- eviction may not touch. Losing earlier conversation costs a follow-up
        its context; losing the question costs the answer.
        """
        ceiling = self._context_tokens - RESERVE_TOKENS
        used = (
            budget.since(self._anchor[1], messages, self._anchor[0])
            if self._anchor is not None
            else budget.transcript_tokens(self._system, messages, specs)
        )
        if used <= ceiling:
            return used
        over = used - ceiling
        # Clearing aims at the target; the harsher moves below only ever aim at the
        # overage, because losing history costs more than losing a stale result.
        freed = budget.microcompact(
            messages,
            keep_recent=KEEP_RECENT_RESULTS,
            need=used - int(ceiling * COMPACT_TARGET),
        )
        if freed < over:
            freed += budget.evict(messages, need=over - freed, keep_last=pinned)
        if freed < over:
            # Down to the last look: better a thin transcript than a refused request.
            freed += budget.microcompact(messages, keep_recent=1, need=over - freed)
        emit("compact", before=used, after=used - freed, freed=freed)
        self._anchor = None  # the counted prefix is not what it was
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
        """One model call, retried once against a smaller transcript if the provider refuses.

        The estimate is an estimate; when the provider disagrees with it the answer is
        to make room and ask again, not to lose the run.
        """
        try:
            return await self._model.complete(
                system=self._system,
                messages=messages,
                tools=specs if tools else (),
                on_text=on_text,
            )
        except Exception as exc:
            # A provider that refuses for size usually says what the size was. Believe
            # it over the configured number, which is only ever an assumption.
            stated = getattr(exc, "context_limit", 0)
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
                # Nothing to give up and nothing learned: asking again would fail the
                # same way. Learning the real window is itself worth one more attempt,
                # since every turn after this one is now measured against it.
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
        """One last turn with the tools withdrawn, so a spent run still answers.

        Without this a run that ran out mid-search returns whatever prose came with its
        last tool call -- usually nothing at all, even when it had already opened every
        document the answer needed.
        """
        nudge = self._out_of_budget
        if owed:
            # It wrote these down itself; naming them back is what stops a wide question
            # from being answered as though it had been a narrow one.
            nudge += "\n\n아직 못 끝낸 것: " + " / ".join(owed)
        messages.append(Message("user", nudge))
        self._fit(messages, (), emit, pinned=pinned + 1)
        try:
            reply = await self._complete(messages, (), emit, on_text=on_text, tools=False)
        except Exception as exc:  # the run is over either way; do not hide what it found
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
        except Exception as exc:  # a tool failing must not crash the loop
            return f"Error running '{call.name}': {exc}", "tool_error"
        return budget.clip(result, limit_chars=self._result_max_chars), "tool_result"
