"""Redrawing the whole collection at once, which no folder can do from inside itself.

A folder cannot see its siblings. 금융 at the root and 금융업 six levels down are the
same subject in two places and neither can be shown the other, so the operation that
fixes that has to stand outside every folder (ADR-0018, docs/spec/maintenance.md 5).

Two questions only, and neither of them reads a document:

*Design* is asked once, from the subject vocabulary the cards already carry. It is the
one call that decides the top of the tree, so it is the one call worth spending the
whole collection's evidence on -- and it costs the same whether the collection holds
three hundred documents or thirty thousand.

*Assignment* is one closed choice per folder, and one per document still loose at the
root. Folders move whole, so a subtree of four hundred documents costs exactly the same
question as a folder of two.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from bismuth.ports.llm import Prompt
from bismuth.prompts.subdivision import in_their_language

_DESIGN_SYSTEM = """\
You are looking at everything a collection is about, and drawing the top of its tree.

You are given the subjects its documents carry, most common first, and the folders that
stand there now. You are NOT given the documents: this is a question about the shape of
the whole collection, and titles would pull you toward what the documents are made of.

**First the QUESTION.** One property, asked as a question a reader could ask of any
document here. Every top-level folder will be one answer to it. Ask the one where a
handful of answers cover the whole list of subjects above: several subjects to each
answer, and none left without one.

Not a property nearly everything answers the same way -- it sorts nothing. Not one where
every document has its own answer, such as which law or work it belongs to, which gives
every folder one document and hands the reader the same list with a step in front of
every entry. Not what the documents ARE -- their kind, their form, their rank, who issued
them, when. Those are known for almost everything, so they fill a tree neatly and scatter
every subject across all of it.

**Then the ANSWERS**, between three and nine of them. Each is a folder name: two or three
words, a subject or a party or an industry, something a reader arrives already wanting.
Together they should cover the subjects listed above without overlapping -- a reader who
picks one must be able to rule out the rest by reading the names alone.

Each answer carries one sentence saying what belongs behind it, written for someone
standing outside who cannot see the documents. Not what happens to be there today; what
would belong there tomorrow.

Nothing is named by what it is not. No name meaning assorted, other, general or related:
the reader cannot tell what is inside, and the next arrival always fits it.
"""


class Class(BaseModel):
    """One answer to the collection's question, which becomes one top-level folder."""

    name: str = Field(description="Two or three words. A subject, a party, an industry.")
    sign: str = Field(
        default="",
        max_length=300,
        description="One sentence: what belongs behind this name, to someone outside.",
    )


class Design(BaseModel):
    """The top of the tree, decided from the whole collection at once.

    The question before the answers: the answers are values of the property the question
    asks about, and written first they are written about nothing.
    """

    question: str = Field(
        default="",
        max_length=200,
        description="One question a reader could ask of any document here, ending in '?'.",
    )
    axis: str = Field(
        default="",
        max_length=100,
        description="The property that question asks about, named in a few words.",
    )
    classes: list[Class] = Field(
        default_factory=list, description="Between three and nine answers to it."
    )
    unsound: list[str] = Field(
        default_factory=list,
        description=(
            "Folders standing here whose names do not say what a reader would find "
            "inside them, exactly as shown. Empty when they all do."
        ),
    )


def build_design(
    *, vocabulary: list[str], folders: list[tuple[str, str, int]], language: str = ""
) -> Prompt:
    """The one call that decides the top of the tree.

    Vocabulary rather than documents: the same evidence the axis step reads, for the same
    reason. Shown titles, that step chose 시행규칙이 규정한 거래 유형 -- the kind of
    instrument, read straight off the names.
    """
    listed = ", ".join(vocabulary)
    standing = (
        "\n".join(
            f"  {name}/  ({count} documents)" + (f" — {note}" if note else "")
            for name, note, count in folders
        )
        or "  (none)"
    )
    return Prompt(
        system=_DESIGN_SYSTEM,
        user=in_their_language(
            f"WHAT THIS COLLECTION IS ABOUT ({len(vocabulary)} subjects, most common "
            f"first):\n{listed}\n\nWHAT STANDS AT THE TOP NOW:\n{standing}",
            language,
        ),
    )


_ASSIGN_SYSTEM = """\
The top of a library has been redrawn, and you are placing ONE thing under it. Reply with
exactly one of the handles offered, or STAY, and nothing else.

STAY when nothing offered describes it. It keeps the place it has, which is safe: a wrong
answer here puts a whole shelf where a reader will not look for it.

Judge against the sign, not the name alone -- a name is two or three words and will
suggest more than it means.
"""


def build_assignment(
    *, subject: str, note: str, count: int, classes: list[tuple[str, str, str]]
) -> Prompt:
    """One closed choice, for a folder or for a document still loose at the root.

    A folder moves whole, so this question costs the same whether it holds four hundred
    documents or two -- which is what bounds the pass to the number of folders rather
    than the number of documents.
    """
    offered = "\n".join(f"  [{handle}] {name}/ — {sign}" for handle, name, sign in classes)
    return Prompt(
        system=_ASSIGN_SYSTEM,
        user=(
            f"THE NEW TOP-LEVEL FOLDERS:\n{offered}\n\n"
            f"WHAT IS BEING PLACED: {subject}\n"
            + (f"ITS SIGN: {note}\n" if note else "")
            + (f"IT HOLDS {count} document(s).\n" if count else "")
        ),
    )
