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

from agentkit import Agent, ChatModel, Message
from agentkit.loop import OnEvent, OnText

from bismuth.ports.vault import Vault
from bismuth.services.agent import build_read_tools
from bismuth.services.charters import CharterService

logger = logging.getLogger(__name__)

SELF_TOOLS: tuple[str, ...] = ()
"""Optional run-state tools available to the librarian."""

LOW_BUDGET = """About {share:.0%} of the budget remains.

Check any unresolved part now. When the budget is exhausted, answer without tools and \
state what could not be verified."""
"""Optional low-budget warning."""

OUT_OF_BUDGET = """No budget remains for tool calls. Answer now from the evidence found.

Cite the document and page for verified claims. Clearly identify anything not verified."""
"""Instruction used for the final answer after budget exhaustion."""

SYSTEM_CHAT = """\
You are the librarian for this vault. Answer by searching the vault directly.

The vault is a folder tree. Each document has a same-named `.md` sidecar containing its \
full text in page sections such as `### 12쪽`, preceded by title, type, topics, entities, \
and summary. Each folder may have `_folder.md` describing what belongs there.

Use the tools as follows:

* Use `tree` to inspect the folder structure.
* Use `read_note` to understand a folder and narrow the search.
* Use `grep` to locate relevant passages. Search a folder recursively or one document.
* Use `read` with line ranges to inspect only the relevant passage.

Do not read entire long documents. Narrow the scope, search, and read the matching area.

Do not stop at one document when it delegates, cites, or refers to another document, \
appendix, or schedule. Follow the reference and verify the corresponding passage. Also \
check the delegating document for limits, conditions, and exceptions. If the referenced \
material is absent, say so instead of substituting a similar item.

For multipart questions, gather evidence for every part before answering. State which \
parts could not be verified.

Answer in the user's language. Cite the document and page for each supported claim. \
Disambiguate duplicate document names. If the vault lacks evidence, say so; clearly \
separate any general knowledge from vault-backed claims. Use prior turns to resolve \
follow-up questions. For questions about the vault itself, rely on `tree` and folder notes.\
"""


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
