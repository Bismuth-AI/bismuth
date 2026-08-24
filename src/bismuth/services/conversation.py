"""Answering questions about the vault, one turn after another.

The tree the rest of Bismuth builds exists to be walked by something with only ``ls``,
``grep`` and ``read``. This is that something, pointed at a person's question instead of
at a filing decision: it reads folder names to narrow down, greps the sidecars to find
where a thing is said, and reads the few lines around a hit rather than whole documents.

Follow-up questions rely on the transcript for context. Conversations are held in memory
and keyed by ID because the vault may change between processes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from bismuth.agentkit import Agent, ChatModel, Message
from bismuth.agentkit.loop import OnEvent, OnText
from bismuth.ports.vault import Vault
from bismuth.prompts.conversation import OUT_OF_BUDGET, SYSTEM_CHAT
from bismuth.services.agent import build_read_tools
from bismuth.services.charters import CharterService

logger = logging.getLogger(__name__)

SELF_TOOLS: tuple[str, ...] = ()
"""Optional run-state tools available to the librarian."""


@dataclass(slots=True)
class Turn:
    """One exchange, kept so the next question can lean on it."""

    question: str
    answer: str
    tools: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Conversation:
    id: str
    messages: list[Message] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)


class ConversationService:
    """Chat over the vault: the same read-only tools, asked a person's question."""

    def __init__(
        self,
        *,
        model: ChatModel,
        vault: Vault,
        charters: CharterService,
        context_tokens: int = 64_000,
        budget_tokens: int = 400_000,
    ) -> None:
        self._model = model
        self._vault = vault
        self._charters = charters
        self._context_tokens = context_tokens
        self._budget_tokens = budget_tokens
        self._open: dict[str, Conversation] = {}

    def start(self) -> Conversation:
        conversation = Conversation(id=uuid.uuid4().hex[:12])
        self._open[conversation.id] = conversation
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        return self._open.get(conversation_id)

    def forget(self, conversation_id: str) -> None:
        self._open.pop(conversation_id, None)

    async def ask(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        on_event: OnEvent | None = None,
        on_text: OnText | None = None,
    ) -> tuple[Conversation, str]:
        """Answer one question in the context of everything said before it."""
        conversation = (
            self.get(conversation_id or "") if conversation_id else None
        ) or self.start()
        agent = Agent(
            model=self._model,
            tools=build_read_tools(self._vault, self._charters),
            system=SYSTEM_CHAT,
            context_tokens=self._context_tokens,
            budget_tokens=self._budget_tokens,
            out_of_budget=OUT_OF_BUDGET,
            self_tools=SELF_TOOLS,
            on_event=on_event,
        )
        result = await agent.run(question, history=conversation.messages, on_text=on_text)
        conversation.messages = result.messages
        conversation.turns.append(
            Turn(
                question=question,
                answer=result.text,
                tools=[
                    str(event.data.get("name"))
                    for event in result.events
                    if event.kind == "tool_call"
                ],
            )
        )
        logger.info(
            "answered in %d turn(s), %d tool call(s), ~%d tokens (%s)",
            result.turns,
            sum(1 for event in result.events if event.kind == "tool_call"),
            result.spent,
            result.stopped,
        )
        return conversation, result.text
