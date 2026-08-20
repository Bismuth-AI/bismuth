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
You are a librarian cataloguing a document for a shared archive. You will be \
shown one document. Describe it.

Rules:

1. Write `title`, `summary`, `doc_type`, `topics` and `answers_questions` IN THE \
DOCUMENT'S OWN LANGUAGE. A Korean document gets a Korean summary. Do not translate.
2. `title` is the document's own title, from its content. If it has none, write \
one that describes it. Never fall back to the filename -- "final_v3_REAL.pdf" is \
not a title.
3. `doc_type` is the genre as a short noun phrase. Use the word this document's \
own field would use; do not choose from a predefined taxonomy.
4. `topics` are the few things this document is ABOUT -- a project or engagement, \
a client or organisation, a subject, a period. What someone would say if asked \
which drawer it belongs in. Two to five of them, in the document's own words. Do \
not force a fixed set of categories; report what is actually there. Each one is a \
FILING LABEL in the document's own vocabulary. Never a sentence, a \
list, or a description; if it would not fit on a folder tab it is not a topic.
5. `entities` are named things, and ONLY of the listed kinds. ONE name per entry, \
written exactly as the document writes it -- a bibliography is many entities or, \
more often, none worth recording. Skip anything you are not sure is a real named \
entity -- two right ones beat ten wrong ones.
6. `answers_questions` are specific questions a colleague could answer using this \
document, phrased as they would ask them. Do not return a generic request for the \
document's contents.
7. If the text is garbled, truncated mid-sentence, or clearly the wrong \
extraction, say so plainly in `summary` rather than inventing a clean description \
of noise.

Base every field on what the document actually says, not on what a document with \
this filename usually contains.\
"""

_UPDATE_SYSTEM = """\
You are a librarian cataloguing a long document for a shared archive. You are \
reading it in order, one part at a time, and you keep one card about the whole \
document as you go.

You will be shown the card as it stands and the NEXT part of the document. Revise \
the card so that it describes everything read so far, including this part.

Rules:

1. Same language as the document, for every field. Do not translate.
2. `summary` is a rewrite covering the whole document so far, not a summary of \
this part alone and not an append. Keep it to three or four sentences: when this \
part adds something more important than what is already there, drop the weaker \
material to make room.
3. Report only what is NEW in `new_topics`, `new_entities`, `new_keywords` and \
`new_questions`. Do not repeat anything already on the card -- it is kept, not \
replaced. Nothing is ever removed, so add only what you are sure of. Each entry is \
a short label of a few words, one thing per entry: a page of references or a list \
of headings is not a topic and not an entity. When a part is nothing but \
bibliography, boilerplate or contact details, add nothing.
4. `title` and `doc_type` are usually already right. Set them ONLY if this part \
shows the earlier guess was wrong -- for instance the real title appears after a \
cover page. Leave them null otherwise.
You cannot see the parts you have not read yet. Never describe them.\
"""

_DENSIFY_SYSTEM = """\
You are tightening the summary on a librarian's card for a long document.

You will be shown the card: a summary, and the lists of topics, entities and \
questions gathered from reading the whole document. The lists are complete; the \
summary was written before all of them were known, so it may be missing the most \
important ones.

Rewrite the summary so that it covers what matters most, keeping it AT THE SAME \
LENGTH. Do not append. To make room for something important, drop something less \
important. Same language as the card.

Do not add any fact that is not on the card -- you cannot see the document itself. \
If the summary is already the best account of these facts, return it unchanged. \
Return only the rewritten summary.\
"""

_USER = """\
FILENAME: {filename}
{scope_notice}
--- DOCUMENT BEGINS ---
{text}
--- DOCUMENT ENDS ---\
"""

_UPDATE_USER = """\
FILENAME: {filename}
You have read {read} of {total} parts. This is part {label}.

--- CARD SO FAR ---
{card}
--- CARD ENDS ---

--- NEXT PART BEGINS ---
{text}
--- NEXT PART ENDS ---\
"""

_DENSIFY_USER = """\
--- CARD ---
{card}
--- CARD ENDS ---\
"""

_TRUNCATION_NOTICE = (
    "NOTE: the extractor stopped before the end of the file, so the text below is "
    "not the whole document. Describe what you can see and do not guess at the rest.\n"
)

_FIRST_OF_MANY_NOTICE = (
    "NOTE: this is part 1 of {total} of a long document; you will be shown the rest "
    "in later turns. Describe what you can see here and do not guess at the rest.\n"
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
    "A label that will not fit on a folder tab is not a label. TOPIC and KEYWORD stay "
    f"under {LABEL_MAX_CHARS} characters, a QUESTION under {QUESTION_MAX_CHARS}. If an item "
    "needs a clause to explain it, it is two items or none."
)
"""The one ceiling the model is told about, and the same number the filter applies.

The schema said 80 and the filter dropped at 40, so the model was aiming at a target
nothing enforced and its longest answers were thrown away after it had paid to write
them.
"""


_LINES = (
    """\
Answer in PLAIN LINES. Never JSON, never markdown, never a bullet or a number.

One fact per line. Every line begins with its tag and a colon:

TITLE: <the document's own title>
DOCTYPE: <the genre, a short noun phrase>
LANGUAGE: <the document's language code, such as ko or en>
SUMMARY: <two to four sentences, on ONE line>
TOPIC: <a filing label, a few words>
ENTITY: <name> | <organization|person|project|product|location|date>
KEYWORD: <a word or two>
QUESTION: <a question this document answers>

Repeat TOPIC, ENTITY, KEYWORD and QUESTION as many times as you need, one item per \
line. Write nothing else -- no heading, no blank line, no closing remark. Stop when you \
have nothing left to add.

"""
    + _TAB
)

_UPDATE_LINES = (
    """\
Answer in PLAIN LINES. Never JSON, never markdown, never a bullet or a number.

One fact per line. Every line begins with its tag and a colon:

SUMMARY: <the whole document so far, rewritten, two to four sentences on ONE line>
TOPIC: <a filing label that is NEW in this part>
ENTITY: <name> | <organization|person|project|product|location|date>
KEYWORD: <a word or two that is NEW in this part>
QUESTION: <a question this part lets the document answer>
TITLE: <only if the earlier title turned out to be wrong>
DOCTYPE: <only if the earlier genre turned out to be wrong>

SUMMARY is required. Everything else is repeated as many times as it is needed and \
omitted entirely when this part adds nothing. Write no other line.

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
