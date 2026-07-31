"""Reading a document and saying what it is; the only prompt that sees raw document text."""

from __future__ import annotations

from pydantic import BaseModel, Field

from bismuth.domain.document import Entity
from bismuth.ports.llm import Prompt

SYSTEM = """\
You are a librarian cataloguing a document for a shared archive. You will be \
shown the beginning of one document. Describe it.

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
not force a fixed set of categories; report what is actually there.
5. `entities` are named things, and ONLY of the listed kinds. Write each name \
exactly as the document writes it. Skip anything you are not sure is a real named \
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

_USER = """\
FILENAME: {filename}
{truncation_notice}
--- DOCUMENT BEGINS ---
{text}
--- DOCUMENT ENDS ---\
"""

_TRUNCATION_NOTICE = (
    "NOTE: this is only the beginning of a longer document. Describe what you can "
    "see and do not guess at the rest.\n"
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


def build(*, filename: str, text: str, truncated: bool) -> Prompt:
    return Prompt(
        system=SYSTEM,
        user=_USER.format(
            filename=filename,
            truncation_notice=_TRUNCATION_NOTICE if truncated else "",
            text=text,
        ),
    )
