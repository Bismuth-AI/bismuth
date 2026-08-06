"""Reading a document and saying what it is; the only prompts that see raw document text.

Three prompts, one loop: describe the first window, update the card from each later
window, then close the gap between the accumulated facts and the summary. Nothing here
assumes the document has headings, a table of contents, or any structure at all.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from bismuth.domain.document import DocumentCard, Entity, Window
from bismuth.ports.llm import Prompt

SYSTEM = """\
You are a librarian cataloguing a document for a shared archive. You will be \
shown one document. Describe it.

Rules:

1. Write `title`, `summary`, `doc_type`, `topics` and `answers_questions` IN THE \
DOCUMENT'S OWN LANGUAGE. A Korean document gets a Korean summary. Do not translate.
2. `title` is the document's own title, from its content. If it has none, write \
one that describes it. Never fall back to the filename -- "final_v3_REAL.pdf" is \
not a title.
3. `doc_type` is the genre, as a short noun phrase: contract, proposal, meeting \
notes, invoice, spec. Use the word this document's own field would use.
4. `topics` are the few things this document is ABOUT -- a project or engagement, \
a client or organisation, a subject, a period. What someone would say if asked \
which drawer it belongs in. Two to five of them, in the document's own words. Do \
not force a fixed set of categories; report what is actually there. Each one is a \
FILING LABEL of a few words -- "지연배상", "생태계서비스 평가". Never a sentence, a \
list, or a description; if it would not fit on a folder tab it is not a topic.
5. `entities` are named things, and ONLY of the listed kinds. ONE name per entry, \
written exactly as the document writes it -- a bibliography is many entities or, \
more often, none worth recording. Skip anything you are not sure is a real named \
entity -- two right ones beat ten wrong ones.
6. `answers_questions` are questions a colleague could answer using this document, \
phrased as they would ask them. Be specific: "아폴로 지원 계약 기간이 얼마인가?" \
not "이 계약서 내용".
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
bibliography, boilerplate or contact details, add nothing and say so in `note`.
4. `title` and `doc_type` are usually already right. Set them ONLY if this part \
shows the earlier guess was wrong -- for instance the real title appears after a \
cover page. Leave them null otherwise.
5. `contributed` is false when this part told you nothing new about the document \
-- a page of boilerplate, a signature block, repeated headers, garbled extraction. \
Say so rather than inventing a difference.
6. `note` is one short line, in English, for a developer reading the log: what this \
part actually was. "clause 7-12, payment terms", "blank cover page", "OCR noise".

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
If the summary is already the best account of these facts, return it unchanged and \
leave `absorbed` empty.

`absorbed` lists the card items you pulled into the summary, exactly as they are \
written on the card.\
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
    topics: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="The few things this document is about, in its own words.",
    )
    entities: list[Entity] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    answers_questions: list[str] = Field(default_factory=list, max_length=6)


class CardUpdate(BaseModel):
    """What one further part of a document changes about the card."""

    summary: str = Field(description="The whole document so far, rewritten. Not an append.")
    contributed: bool = Field(description="False when this part added nothing.")
    note: str = Field(default="", description="One line for the log: what this part was.")
    title: str | None = Field(
        default=None, description="Only when the earlier title turned out to be wrong."
    )
    doc_type: str | None = Field(
        default=None, description="Only when the earlier genre turned out to be wrong."
    )
    new_topics: list[str] = Field(default_factory=list, max_length=6)
    new_entities: list[Entity] = Field(default_factory=list, max_length=20)
    new_keywords: list[str] = Field(default_factory=list, max_length=12)
    new_questions: list[str] = Field(default_factory=list, max_length=6)


class DensifiedSummary(BaseModel):
    """A summary rewritten to carry the facts that matter, at unchanged length."""

    summary: str
    absorbed: list[str] = Field(
        default_factory=list, description="Card items pulled into the summary."
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
        system=SYSTEM,
        user=_USER.format(filename=filename, scope_notice=notice, text=window.text),
    )


def build_update(*, filename: str, window: Window, card: DocumentCard, read: int) -> Prompt:
    """Fold one further window into the card built from the earlier ones."""
    return Prompt(
        system=_UPDATE_SYSTEM,
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
