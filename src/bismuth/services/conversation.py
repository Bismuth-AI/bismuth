"""Answering questions about the vault, one turn after another.

The tree the rest of Bismuth builds exists to be walked by something with only ``ls``,
``grep`` and ``read``. This is that something, pointed at a person's question instead of
at a filing decision: it reads folder names to narrow down, greps the sidecars to find
where a thing is said, and reads the few lines around a hit rather than whole documents.

Follow-up questions rely on the transcript for context. Live conversations are held in
memory and keyed by ID; each answered turn is also written to a transcript store, so a
conversation can be found and reopened after the process that held it is gone.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bismuth.agentkit import Agent, ChatModel, Message
from bismuth.agentkit.loop import OnEvent, OnText
from bismuth.domain.transcript import Transcript, TranscriptSummary, TranscriptTurn
from bismuth.ports.transcripts import TranscriptStore
from bismuth.ports.vault import Vault
from bismuth.prompts.conversation import OUT_OF_BUDGET, SYSTEM_CHAT
from bismuth.services.agent import build_read_tools
from bismuth.services.charters import CharterService

logger = logging.getLogger(__name__)

SELF_TOOLS: tuple[str, ...] = ()
"""Optional run-state tools available to the librarian."""


@dataclass(slots=True)
class Conversation:
    id: str
    messages: list[Message] = field(default_factory=list)
    turns: list[TranscriptTurn] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def transcript(self) -> Transcript:
        return Transcript(
            id=self.id,
            turns=list(self.turns),
            started_at=self.started_at,
            updated_at=datetime.now(UTC),
        )


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
        transcripts: TranscriptStore | None = None,
    ) -> None:
        self._model = model
        self._vault = vault
        self._charters = charters
        self._transcripts = transcripts
        self._context_tokens = context_tokens
        self._budget_tokens = budget_tokens
        self._open: dict[str, Conversation] = {}

    def start(self) -> Conversation:
        conversation = Conversation(id=uuid.uuid4().hex[:12])
        self._open[conversation.id] = conversation
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        """A live conversation, reopened from its stored transcript when it is not held."""
        if live := self._open.get(conversation_id):
            return live
        stored = self._transcripts.get(conversation_id) if self._transcripts else None
        if stored is None:
            return None
        return self._reopen(stored)

    def history(self, *, limit: int | None = None) -> list[TranscriptSummary]:
        return self._transcripts.list(limit=limit) if self._transcripts else []

    def forget(self, conversation_id: str) -> None:
        self._open.pop(conversation_id, None)
        if self._transcripts:
            self._transcripts.delete(conversation_id)

    def _reopen(self, stored: Transcript) -> Conversation:
        """Rebuild a conversation from what was said. Tool traffic is not replayed."""
        messages: list[Message] = []
        for turn in stored.turns:
            messages.append(Message(role="user", content=turn.question))
            messages.append(Message(role="assistant", content=turn.answer))
        conversation = Conversation(
            id=stored.id,
            messages=messages,
            turns=list(stored.turns),
            started_at=stored.started_at,
        )
        self._open[conversation.id] = conversation
        return conversation

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
            TranscriptTurn(
                question=question,
                answer=result.text,
                tools=[
                    str(event.data.get("name"))
                    for event in result.events
                    if event.kind == "tool_call"
                ],
            )
        )
        if self._transcripts:
            self._transcripts.save(conversation.transcript())
        logger.info(
            "answered in %d turn(s), %d tool call(s), ~%d tokens (%s)",
            result.turns,
            sum(1 for event in result.events if event.kind == "tool_call"),
            result.spent,
            result.stopped,
        )
        return conversation, result.text
