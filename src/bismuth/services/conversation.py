"""Answering questions about the vault, one turn after another.

The tree the rest of Bismuth builds exists to be walked by something with only ``ls``,
``grep`` and ``read``. This is that something, pointed at a person's question instead of
at a filing decision: it reads folder names to narrow down, greps the sidecars to find
where a thing is said, and reads the few lines around a hit rather than whole documents.

Multi-turn because a second question is rarely a whole one. "그중 최신은?" carries no
subject; the transcript is what supplies it. Conversations are held in memory and keyed by
id -- a vault is one person on one machine, and an answer that outlives the process would
have to be re-grounded against a tree that may have changed underneath it anyway.
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
"""Which of the loop's own tools the librarian gets. None, as measured.

``plan`` and the budget warning both come from codex, and both were tried here: plan with
the warning at 30% and at 15% of budget left, and plan alone. All three scored below the
build without them -- 0.956, 0.952, 0.945 against 0.976 -- and the loss fell in the very
categories plan was meant to help (synthesis 0.89 -> 0.78, cross-law 0.96 -> 0.88). It did
what it was built for on individual questions (L5-012 0.60 -> 1.00, L8-005 0.75 -> 1.00);
it just cost more calls elsewhere than those were worth.

The budget warning turned out to duplicate work already done. It prevents a run from being
cut off, and ``answer_anyway`` already rescues one that was: the 33 truncated runs without
the warning all still answered. Two guards on one failure, and the earlier one is free.

Kept switchable rather than deleted -- the measurement is worth more standing next to the
code than in a document nobody opens, and a harder question set could turn it around."""

LOW_BUDGET = """이 질문에 쓸 예산의 약 {share:.0%}가 남았다.

아직 확인하지 못한 갈래가 있다면 **지금 그것부터** 본다. 예산이 다하면 도구 없이 답만 쓰게 \
되고, 그때까지 못 본 것은 못 봤다고 적어야 한다."""
"""The warning, once, while there is still budget to act on it.

Taken from codex's ``reminder_threshold_tokens``: an agent that learns it is out of
room at the moment it runs out has learned nothing it can use."""

OUT_OF_BUDGET = """도구를 더 쓸 예산이 없다. **지금까지 찾은 것만으로 지금 답하라.**

확인한 것은 근거(문서·쪽)와 함께 그대로 말하고, 확인하지 못한 것은 확인하지 못했다고 밝힌다. 아무 말도 하지 않고 끝내지 마라."""
"""What the agent is told when its budget runs out.

Without it a search that ran out mid-way returns whatever prose came with its last tool
call -- which is usually nothing, even when it had already opened every document the
answer needed."""

SYSTEM_CHAT = """\
너는 이 서고를 관리하는 사서다. 사용자가 묻는 것에 대해, **서고를 직접 뒤져서** 답한다.

서고는 폴더 트리다. 문서마다 그 옆에 같은 이름의 `.md` 사이드카가 있고, 거기에 그 문서의 \
**본문 전체**가 쪽 단위(`### 12쪽`)로 들어 있다. 맨 앞에는 제목·종류·주제·개체·요약이 있다. \
폴더마다 `_folder.md` 가 있어 그 폴더가 무엇을 담는지 한 문장으로 말해준다.

도구를 이렇게 쓴다:

* `tree` 로 전체 모양을 본다. 폴더 이름이 곧 분류다.
* `read_note` 로 어떤 폴더가 무엇을 담는지 확인하고 범위를 좁힌다.
* `grep` 으로 그 말이 **어디서** 나오는지 찾는다. 폴더를 주면 그 아래 전부를, 문서 하나를 \
주면 그 문서 안만 뒤진다. 긴 문서는 훑지 말고 `grep` 으로 자리부터 찾아라.
* `read` 로 찾은 자리 앞뒤만 읽는다. 줄 번호로 범위를 준다.

**문서를 통째로 읽지 마라.** 좁히고, 찾고, 그 자리만 읽는 것이 이 서고의 사용법이다. \
사이드카 하나가 십만 자를 넘기도 한다.

**한 문서에서 멈추지 마라.**

* 문서가 **다른 문서로 미루면**(구체적인 내용을 다른 규정에 위임하거나, 다른 문서를 \
가리키거나, 별표·부록을 참조하면) 미룬 쪽만 보고 답하지 마라. **가리켜진 문서를 찾아가서 \
대응하는 자리를 확인**하고, 그 자리의 내용을 근거로 답한다.
* 반대 방향도 같다. 위임받은 쪽을 읽고 있다면, **위임한 쪽**에 상한·전제·예외가 있는지 \
확인한다.
* 찾아갔는데 **대응하는 자리가 정말 없으면 "없다"고 말한다.** 번호가 비슷하거나 이름이 \
비슷한 다른 자리를 대응이라고 내세우지 마라. 없다는 것도 근거 있는 답이다.

**질문이 여러 갈래면 갈래마다 근거를 확보하라.** "모두", "각각", "비교하라", "A와 B" 는 \
여러 문서를 요구한다. 하나를 깊게 파다 나머지를 빠뜨리지 말고, 먼저 갈래를 세어 두고 \
각각에 대해 찾은 뒤 답한다. 끝내 못 찾은 갈래는 못 찾았다고 밝힌다.

답할 때:

* **한국어로**, 사용자가 물은 것에 곧장 답한다.
* **근거를 밝힌다** — 어느 문서의 몇 쪽인지. 같은 이름을 단 문서가 서고에 여럿이면 \
**어느 것인지까지** 밝힌다. 하나뿐이면 이름만으로 충분하다. 쪽 번호는 사이드카 본문에 \
`### N쪽` 이 박혀 있으니 읽은 자리의 것을 그대로 쓰면 된다.
* **서고에 없으면 없다고 말한다.** 지어내지 마라. 서고 밖의 일반 지식으로 답해야 할 때는 \
"이 서고에는 근거가 없고, 일반적으로는 …" 이라고 그 경계를 분명히 한다.
* 사용자가 이어서 묻는 말은 앞의 대화를 가리킨다. 앞 답변에서 무엇을 말했는지 기억하고 \
그 위에서 답한다.
* 서고 자체에 대한 질문(무엇이 들어 있나, 어떻게 분류돼 있나)도 같은 방식으로 답한다 -- \
`tree` 와 폴더 설명이 그 답이다.\
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
