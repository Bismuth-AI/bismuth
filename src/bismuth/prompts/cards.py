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

#: Folder-tab labels must stay short enough to scan and index reliably.
Label = Annotated[str, StringConstraints(max_length=LABEL_MAX_CHARS)]

SYSTEM = """\
You are a librarian cataloging one document for a shared library. Describe what the
document is.

Rules:
1. Write `title`, `summary`, `doc_type`, `topics`, and `answers_questions` in the
   document's own language. If the document is Korean, its summary must also be Korean.
   Do not translate.
2. `title` is the title the document gives itself. Find it in the content. If the document
   has no title, create a descriptive one from the content. Never substitute the filename;
   a filename such as `final_v3_REAL.pdf` is not a title.
3. `doc_type` is a short noun phrase naming what kind of document this is. Use the term
   actually used in the document's field. Do not choose from a predetermined taxonomy.
4. `topics` are two to five things the document is about: an activity, organization,
   subject, or period. They are answers to "which drawer would this go in?" Use the
   document's own vocabulary and do not force it into predefined categories. Each topic
   is a short folder-tab label, not a sentence, list, explanation, or document title.
5. `entities` are explicitly named things of the allowed kinds only. Put one name in each
   item and preserve the document's wording. A references page may contain many names;
   most documents may contain none worth extracting. Omit anything that is not clearly a
   proper name. Two correct entities are better than ten incorrect ones.
6. `answers_questions` are concrete questions a colleague could answer after reading this
   document, phrased as that colleague might naturally ask them. Do not use vague questions
   such as "what is this document about?"
7. If the extracted text is damaged, cut off mid-sentence, or clearly incorrect, state
   that directly in `summary`. Do not turn extraction noise into a clean invented account.

Ground every field in the supplied text, not in assumptions based on the filename.\
"""

_UPDATE_SYSTEM = """\
You are cataloging a long document for a shared library. You are reading it from the
beginning one section at a time while maintaining one card for the whole document.

Given the card so far and the next section, update the card to describe everything read
through this section.

Rules:
1. Keep all content fields in the document's language. Do not translate them.
2. Rewrite `summary` for the entire document read so far. It is not a summary of only this
   section and must not be appended to the old summary. Keep it to three or four sentences.
   If this section adds something more important, remove weaker details to make room.
3. Put only genuinely new items in `new_topics`, `new_entities`, `new_keywords`, and
   `new_questions`. Do not repeat items already on the card. These lists accumulate rather
   than replace earlier values, so add only items you are confident should remain. Each
   item is one short label containing one thing. A references page or list of titles is
   not a topic or entity. Add nothing when a section contains only references, boilerplate,
   or contact details.
4. `title` and `doc_type` are usually already correct. Return a replacement only when this
   section proves the earlier value wrong, such as when the true title appears after a
   cover page. Otherwise omit them.

Unread sections are not visible to you. Never claim anything about them.\
"""

_DENSIFY_SYSTEM = """\
Tighten the catalog summary for a long document.

You will receive a card containing a summary plus topics, entities, and questions gathered
while reading the entire document. The lists are complete, but the summary was written
before all of them were known and may omit something important.

Rewrite the summary so it includes the most important information while keeping the same
length. Do not append text. Remove weaker details to make room for stronger ones. Write in
the card's language.

Add no fact absent from the card because the document itself is not visible. If the
current summary is already the best account of these facts, return it unchanged. Return
only the rewritten summary.\
"""

_USER = """\
FILENAME: {filename}
{scope_notice}
--- DOCUMENT START ---
{text}
--- DOCUMENT END ---\
"""

_UPDATE_USER = """\
FILENAME: {filename}
Read through section {read} of {total}. This section is {label}.

--- CARD SO FAR ---
{card}
--- CARD END ---

--- NEXT SECTION START ---
{text}
--- NEXT SECTION END ---\
"""

_DENSIFY_USER = """\
--- CARD ---
{card}
--- CARD END ---\
"""

_TRUNCATION_NOTICE = (
    "NOTE: Extraction stopped before the end of the file. Describe only the visible text "
    "and do not infer the missing content.\n"
)

_FIRST_OF_MANY_NOTICE = (
    "NOTE: This is the first of {total} sections. Describe only the visible text; later "
    "sections will be supplied separately.\n"
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
    "A label too long for a folder tab is not a label. TOPIC and KEYWORD must be at most "
    f"{LABEL_MAX_CHARS} characters; QUESTION must be at most {QUESTION_MAX_CHARS}. "
    "If an item needs an explanatory clause to make sense, split it into separate items "
    "or omit it."
)


_LINES = (
    """\
Return plain tagged lines, not JSON, Markdown, bullets, or numbering. Use one item per
line and exactly these tags:

TITLE: <the document's own title>
DOCTYPE: <short noun phrase>
LANGUAGE: <language code such as ko or en>
SUMMARY: <two to four sentences on one line>
TOPIC: <short reusable folder label>
ENTITY: <name> | <organization|person|project|product|location|date>
KEYWORD: <one or two words>
QUESTION: <a question this document answers>

Repeat TOPIC, ENTITY, KEYWORD, and QUESTION as needed, one item per line. Return nothing
else: no preface, blank lines, or closing sentence. Stop when there is nothing more to add.

"""
    + _TAB
)

_UPDATE_LINES = (
    """\
Return plain tagged lines, not JSON, Markdown, bullets, or numbering:

SUMMARY: <the whole document so far, two to four sentences on one line>
TOPIC: <new reusable folder label from this section>
ENTITY: <name> | <organization|person|project|product|location|date>
KEYWORD: <new one- or two-word term from this section>
QUESTION: <new question enabled by this section>
TITLE: <only if the earlier title was wrong>
DOCTYPE: <only if the earlier type was wrong>

SUMMARY is required. Repeat other tags as needed, and omit them entirely when this section
adds nothing. Return no other lines.

"""
    + _TAB
)

_KINDS = {kind.value for kind in EntityKind}


def _entity(value: str) -> Entity | None:
    """Parse one ENTITY line while accepting common separators."""
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
    """Split a model's comma-separated items when it ignored the line format."""
    parts = [part.strip() for part in value.split(",")]
    return [part for part in parts if part] if all(parts) and len(parts) > 1 else [value]


def parse_card(text: str) -> ParsedCard:
    """Read tagged lines into the fields a card is made of.

    The caller decides how to handle missing fields. Unknown lines are ignored.
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
