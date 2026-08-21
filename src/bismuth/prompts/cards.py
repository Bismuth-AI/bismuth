"""Reading a document and saying what it is; the only prompts that see raw document text.

Three prompts, one loop: describe the first window, update the card from each later
window, then close the gap between the accumulated facts and the summary. Nothing here
assumes the document has headings, a table of contents, or any structure at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from bismuth.domain.document import (
    LABEL_MAX_CHARS,
    QUESTION_MAX_CHARS,
    DocumentCard,
    Entity,
    EntityKind,
    Window,
)
from bismuth.ports.llm import Prompt

#: A label, not prose. The arrays were bounded and their items were not, so a
#: single item could run away: one keyword came back as
#: 옥외광고물관리법규제특례법규제특례법규제특례… until the repetition breaker cut the
#: stream, and 79 of 300 cards needed a retry in one run. Measured over 3,661
#: topics and 6,568 keywords from a real vault, the longest honest value is 40
#: characters and the 95th percentile is 23, so this refuses only the runaway.
#: SPEC.md 2.1 forbids ceilings on *semantic* fields -- summary keeps none.
Label = Annotated[str, StringConstraints(max_length=LABEL_MAX_CHARS)]

SYSTEM = """\
너는 공용 서고에 들어갈 문서를 목록화하는 사서다. 문서 하나를 보여줄 테니, 그것이 무엇인지 \
적어라.

규칙:

1. `title`, `summary`, `doc_type`, `topics`, `answers_questions` 는 **문서 자신의 말로** \
쓴다. 한국어 문서면 요약도 한국어다. 번역하지 마라.
2. `title` 은 문서가 스스로 달고 있는 제목이고, 본문에서 찾는다. 제목이 없으면 그 문서를 \
설명하는 제목을 지어라. 파일 이름으로 대신하지 마라 -- "final_v3_REAL.pdf" 는 제목이 아니다.
3. `doc_type` 은 어떤 종류의 문서인지를 짧은 명사구로 쓴다. 이 문서가 속한 분야가 실제로 \
쓰는 말을 써라. 미리 정해진 분류표에서 고르지 마라.
4. `topics` 는 이 문서가 **무엇에 관한 것인지** 몇 가지다 -- 어떤 일, 어떤 조직, 어떤 주제, \
어떤 시기. "이건 어느 서랍에 넣지?" 하고 물었을 때 나올 대답이다. 두 개에서 다섯 개, 문서가 \
쓰는 말 그대로. 정해진 범주에 억지로 맞추지 말고 실제로 있는 것을 적어라. 하나하나가 문서 \
자신의 어휘로 된 **폴더 이름표**다. 문장도 목록도 설명도 아니다 -- 폴더 탭에 안 들어갈 \
길이면 그건 주제가 아니다.
5. `entities` 는 이름이 붙은 것들이고, **나열된 종류만** 해당한다. 한 항목에 이름 하나, \
문서가 적은 그대로 쓴다 -- 참고문헌 목록은 개체가 아주 많거나, 대개는 적을 것이 하나도 없다. \
진짜 고유한 이름인지 확신이 없으면 빼라. 맞는 것 둘이 틀린 것 열보다 낫다.
6. `answers_questions` 는 이 문서를 보면 동료에게 답해줄 수 있는 구체적인 질문이고, 동료가 \
물어볼 법한 말투로 쓴다. "이 문서 내용이 뭐야" 같은 두루뭉술한 질문은 쓰지 마라.
7. 글자가 깨졌거나, 문장 중간에 잘렸거나, 명백히 잘못 추출된 텍스트라면 `summary` 에 \
그렇다고 그대로 써라. 잡음을 깔끔한 설명으로 지어내지 마라.

모든 항목의 근거는 문서가 실제로 하는 말이다. 이런 파일 이름의 문서가 보통 무엇을 담는지가 \
아니다.\
"""

_UPDATE_SYSTEM = """\
너는 공용 서고에 들어갈 긴 문서를 목록화하는 사서다. 그 문서를 앞에서부터 한 부분씩 읽고 \
있고, 읽는 내내 문서 전체에 대한 카드 하나를 손에 들고 고쳐 나간다.

지금까지의 카드와 문서의 **다음 부분**을 보여줄 테니, 이번 부분까지 포함해 지금까지 읽은 \
전부를 설명하도록 카드를 고쳐라.

규칙:

1. 모든 항목은 문서와 같은 말로 쓴다. 번역하지 마라.
2. `summary` 는 지금까지의 문서 **전체**를 다시 쓴 것이다. 이번 부분만의 요약이 아니고, \
뒤에 덧붙이는 것도 아니다. 서너 문장으로 유지하라 -- 이번 부분이 이미 적힌 것보다 중요한 \
것을 가져왔다면, 약한 쪽을 덜어내서 자리를 만들어라.
3. `new_topics`, `new_entities`, `new_keywords`, `new_questions` 에는 **새로운 것만** \
적는다. 카드에 이미 있는 것은 되풀이하지 마라 -- 카드는 교체되는 것이 아니라 쌓인다. 한 번 \
들어간 것은 빠지지 않으니 확신이 있는 것만 더해라. 항목 하나는 몇 단어짜리 짧은 이름표이고 \
한 항목에 한 가지만 담는다. 참고문헌 한 쪽이나 제목 나열은 주제도 개체도 아니다. 어떤 \
부분이 참고문헌·상투 문구·연락처뿐이라면 아무것도 더하지 마라.
4. `title` 과 `doc_type` 은 대개 이미 맞다. 이번 부분이 앞의 판단이 틀렸음을 보여줄 때만 \
-- 이를테면 표지 뒤에 진짜 제목이 나올 때만 -- 새로 써라. 그 외에는 비워 둬라.
아직 읽지 않은 부분은 너에게 보이지 않는다. 그 부분에 대해서는 절대 말하지 마라.\
"""

_DENSIFY_SYSTEM = """\
너는 긴 문서에 대한 사서 카드의 요약을 조인다.

카드를 보여줄 것이다: 요약 하나와, 문서 전체를 읽으며 모은 주제·개체·질문 목록. 목록은 이미 \
다 모였고, 요약은 그것들이 다 알려지기 전에 쓰였다. 그래서 가장 중요한 것이 요약에 빠져 \
있을 수 있다.

가장 중요한 것들이 담기도록 요약을 다시 써라. **길이는 그대로 두어라.** 덧붙이지 마라. \
중요한 것을 넣을 자리는 덜 중요한 것을 덜어내 만들어라. 카드와 같은 말로 쓴다.

카드에 없는 사실은 하나도 더하지 마라 -- 너는 문서 자체를 볼 수 없다. 지금 요약이 이 \
사실들에 대한 최선의 설명이라면 그대로 돌려줘라. 다시 쓴 요약만 답하라.\
"""

_USER = """\
파일 이름: {filename}
{scope_notice}
--- 문서 시작 ---
{text}
--- 문서 끝 ---\
"""

_UPDATE_USER = """\
파일 이름: {filename}
전체 {total} 부분 중 {read} 부분까지 읽었다. 이번은 {label} 부분이다.

--- 지금까지의 카드 ---
{card}
--- 카드 끝 ---

--- 다음 부분 시작 ---
{text}
--- 다음 부분 끝 ---\
"""

_DENSIFY_USER = """\
--- 카드 ---
{card}
--- 카드 끝 ---\
"""

_TRUNCATION_NOTICE = (
    "참고: 추출기가 파일 끝에 닿기 전에 멈췄다. 그래서 아래 텍스트는 문서 전체가 아니다. "
    "보이는 것만 설명하고 나머지는 짐작하지 마라.\n"
)

_FIRST_OF_MANY_NOTICE = (
    "참고: 이것은 긴 문서의 전체 {total} 부분 중 첫 부분이고, 나머지는 다음 차례에 보여줄 "
    "것이다. 여기 보이는 것만 설명하고 나머지는 짐작하지 마라.\n"
)


class CardDraft(BaseModel):
    """What the model returns for one document."""

    title: str = Field(description="The document's own title, in its own language.")
    summary: str = Field(description="Two or three sentences. What it is and what it is for.")
    doc_type: str = Field(description="Short noun phrase for the genre.")
    language: str = Field(description="Language code of the document, e.g. 'ko', 'en'.")
    topics: list[Label] = Field(
        default_factory=list,
        max_length=6,
        description="The few things this document is about, in its own words.",
    )
    entities: list[Entity] = Field(default_factory=list, max_length=20)
    keywords: list[Label] = Field(default_factory=list, max_length=12)
    answers_questions: list[Label] = Field(default_factory=list, max_length=6)


class CardUpdate(BaseModel):
    """What one further part of a document changes about the card."""

    summary: str = Field(description="The whole document so far, rewritten. Not an append.")
    title: str | None = Field(
        default=None, description="Only when the earlier title turned out to be wrong."
    )
    doc_type: str | None = Field(
        default=None, description="Only when the earlier genre turned out to be wrong."
    )
    new_topics: list[Label] = Field(default_factory=list, max_length=6)
    new_entities: list[Entity] = Field(default_factory=list, max_length=20)
    new_keywords: list[Label] = Field(default_factory=list, max_length=12)
    new_questions: list[Label] = Field(default_factory=list, max_length=6)


class DensifiedSummary(BaseModel):
    """A summary rewritten to carry the facts that matter, at unchanged length."""

    summary: str


_TAB = (
    "폴더 탭에 안 들어갈 이름표는 이름표가 아니다. TOPIC 과 KEYWORD 는 "
    f"{LABEL_MAX_CHARS}자, QUESTION 은 {QUESTION_MAX_CHARS}자를 넘기지 마라. 설명하는 절이 "
    "붙어야 뜻이 서는 항목이면, 그건 두 항목이거나 아예 항목이 아니다."
)
"""The one ceiling the model is told about, and the same number the filter applies.

The schema said 80 and the filter dropped at 40, so the model was aiming at a target
nothing enforced and its longest answers were thrown away after it had paid to write
them.
"""


_LINES = (
    """\
답은 **일반 텍스트 줄**로 하라. JSON 도, 마크다운도, 글머리표도, 번호도 쓰지 마라.

한 줄에 한 가지. 모든 줄은 자기 태그와 콜론으로 시작한다. 태그 이름은 아래 그대로 쓴다:

TITLE: <문서 자신의 제목>
DOCTYPE: <어떤 종류의 문서인지, 짧은 명사구>
LANGUAGE: <문서의 언어 코드, 예를 들어 ko 나 en>
SUMMARY: <두 문장에서 네 문장, 반드시 한 줄에>
TOPIC: <폴더 이름표, 몇 단어>
ENTITY: <이름> | <organization|person|project|product|location|date>
KEYWORD: <한두 단어>
QUESTION: <이 문서가 답해주는 질문>

TOPIC, ENTITY, KEYWORD, QUESTION 은 필요한 만큼 되풀이하되 한 줄에 하나씩 쓴다. 그 밖에는 \
아무것도 쓰지 마라 -- 머리말도, 빈 줄도, 맺음말도. 더 쓸 것이 없으면 거기서 멈춰라.

"""
    + _TAB
)

_UPDATE_LINES = (
    """\
답은 **일반 텍스트 줄**로 하라. JSON 도, 마크다운도, 글머리표도, 번호도 쓰지 마라.

한 줄에 한 가지. 모든 줄은 자기 태그와 콜론으로 시작한다. 태그 이름은 아래 그대로 쓴다:

SUMMARY: <지금까지의 문서 전체를 다시 쓴 것, 두 문장에서 네 문장, 반드시 한 줄에>
TOPIC: <이번 부분에서 새로 나온 폴더 이름표>
ENTITY: <이름> | <organization|person|project|product|location|date>
KEYWORD: <이번 부분에서 새로 나온 한두 단어>
QUESTION: <이번 부분 덕분에 이 문서가 답할 수 있게 된 질문>
TITLE: <앞의 제목이 틀렸던 경우에만>
DOCTYPE: <앞의 종류가 틀렸던 경우에만>

SUMMARY 는 반드시 있어야 한다. 나머지는 필요한 만큼 되풀이하고, 이번 부분이 더하는 것이 \
없으면 아예 쓰지 않는다. 다른 줄은 쓰지 마라.

"""
    + _TAB
)

_KINDS = {kind.value for kind in EntityKind}


def _entity(value: str) -> Entity | None:
    """One ENTITY line. Forgiving about the separator, strict about the kind.

    Asked for `name | kind`, the model answered `name [organization]` on the first run.
    A line format has no grammar to hold it to one spelling, so the parser holds the
    meaning instead: whatever bracket it used, the kind is only accepted when it names
    one we have.
    """
    name, kind = value, ""
    for opener, closer in (("|", ""), ("[", "]"), ("(", ")"), (" - ", "")):
        if opener in value:
            name, _, rest = value.partition(opener)
            kind = rest.strip().rstrip(closer).strip().casefold()
            break
    name = name.strip()
    if not name:
        return None
    return Entity(name=name, kind=EntityKind(kind) if kind in _KINDS else EntityKind.ORGANIZATION)


@dataclass(frozen=True, slots=True)
class ParsedCard:
    """What one reply offered, before anything decides what a missing field means."""

    title: str = ""
    doc_type: str = ""
    language: str = ""
    summary: str = ""
    topics: tuple[str, ...] = ()
    entities: tuple[Entity, ...] = ()
    keywords: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()


def _items(value: str) -> list[str]:
    """One tagged line, which is sometimes several items.

    Asked for one per line, the model occasionally writes the whole list on one:
    ``KEYWORD: 온누리상품권, 가맹점, 과징금, 판매대행자``. Read whole, that line is far
    past what fits on a folder tab and the filter drops it -- so eight items were lost in
    one run for being written with commas instead of newlines, which is not a difference
    in what the model found.

    Only a separator, never a rewrite: a value with no comma comes back as itself, and a
    label that genuinely contains one keeps it if splitting would leave an empty piece.
    """
    parts = [part.strip() for part in value.split(",")]
    return [part for part in parts if part] if all(parts) and len(parts) > 1 else [value]


def parse_card(text: str) -> ParsedCard:
    """Read tagged lines into the fields a card is made of.

    Returns what was found and nothing else: the caller decides what a missing title or
    an empty summary means, because that differs between the first window and a later
    one. Unrecognised lines are dropped rather than guessed at -- across 36 replies in
    the bake-off there were none, and a line nobody asked for is not evidence.
    """
    found: dict[str, Any] = {
        "title": "",
        "doc_type": "",
        "language": "",
        "summary": "",
        "topics": [],
        "entities": [],
        "keywords": [],
        "questions": [],
    }
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*\u2022 ").strip()
        tag, separator, value = line.partition(":")
        value = value.strip()
        if not separator or not value:
            continue
        match tag.strip().upper():
            case "TITLE":
                found["title"] = value
            case "DOCTYPE" | "DOC_TYPE" | "TYPE":
                found["doc_type"] = value
            case "LANGUAGE" | "LANG":
                found["language"] = value
            case "SUMMARY":
                found["summary"] = f"{found['summary']} {value}".strip()
            case "TOPIC":
                found["topics"].extend(_items(value))
            case "KEYWORD":
                found["keywords"].extend(_items(value))
            case "QUESTION":
                found["questions"].append(value)
            case "ENTITY":
                if entity := _entity(value):
                    found["entities"].append(entity)
    return ParsedCard(
        title=found["title"],
        doc_type=found["doc_type"],
        language=found["language"],
        summary=found["summary"],
        topics=tuple(found["topics"]),
        entities=tuple(found["entities"]),
        keywords=tuple(found["keywords"]),
        questions=tuple(found["questions"]),
    )


def build(*, filename: str, window: Window, truncated: bool) -> Prompt:
    """Describe the first (or only) window of a document."""
    if truncated and window.total == 1:
        notice = _TRUNCATION_NOTICE
    elif window.total > 1:
        notice = _FIRST_OF_MANY_NOTICE.format(total=window.total)
    else:
        notice = ""
    return Prompt(
        system=SYSTEM + "\n" + _LINES,
        user=_USER.format(filename=filename, scope_notice=notice, text=window.text),
    )


def build_update(*, filename: str, window: Window, card: DocumentCard, read: int) -> Prompt:
    """Fold one further window into the card built from the earlier ones."""
    return Prompt(
        system=_UPDATE_SYSTEM + "\n" + _UPDATE_LINES,
        user=_UPDATE_USER.format(
            filename=filename,
            read=read,
            total=window.total,
            label=window.label,
            card=render_card(card),
            text=window.text,
        ),
    )


def build_densify(*, card: DocumentCard) -> Prompt:
    """Close the gap between the facts gathered from the whole document and the summary."""
    return Prompt(system=_DENSIFY_SYSTEM, user=_DENSIFY_USER.format(card=render_card(card)))


def render_card(card: DocumentCard) -> str:
    """The card as the model sees it between passes. Plain text; the model rewrites prose, not JSON."""
    lines = [
        f"title: {card.title}",
        f"doc_type: {card.doc_type}",
        f"language: {card.language}",
        f"summary: {card.summary}",
        f"topics: {', '.join(card.topics) or '(none)'}",
        f"entities: {', '.join(f'{e.name} [{e.kind.value}]' for e in card.entities) or '(none)'}",
        f"keywords: {', '.join(card.keywords) or '(none)'}",
    ]
    if card.answers_questions:
        lines.append("answers_questions:")
        lines += [f"  - {question}" for question in card.answers_questions]
    else:
        lines.append("answers_questions: (none)")
    return "\n".join(lines)
