"""Asking whether a folder should be divided, and how.

Two prompts, because the question changes once a folder has been divided. Asking an
already-divided folder "how would you divide this?" gets an answer every time, and a
slightly different one each time, so documents move for ever. Asking "does the
division you made still hold?" has a default answer of yes and has to be argued out
of it. See docs/spec/subdivision.md.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from bismuth.ports.llm import Prompt

_SHARED_RULES = """\
Rules:

1. A distinction is worth drawing when it helps someone FIND things, not when it is \
merely true. "Documents from 2023" is true of some of these and helps nobody unless \
the year is how people look. Two groups that a searcher could not tell apart from \
their names are not two groups.
2. Count is not the question. Ten documents about ten subjects should be divided; \
fifty about one subject should not.
3. Name each group for the CLASS it holds, not for one document in it. A name taken \
from a single document can hold that document and nothing else.
4. Names and notes in the DOCUMENT'S OWN LANGUAGE.
5. `note` for each group is what a searcher reads to decide whether to open it. Say \
what distinguishes this group from its siblings, not just what is in it.
6. A document that fits no group stays where it is. That is normal -- leave it out \
of every group rather than forcing it into the nearest one.\
"""

_DIVIDE_SYSTEM = f"""\
You are a librarian looking at one folder in an archive that is still being built. \
Decide whether the documents sitting directly in it should be divided into \
sub-folders.

You are shown what is in this folder. You are NOT shown the documents themselves, \
only what each one is and what it is about.

{_SHARED_RULES}

If there is no distinction worth drawing yet, say so and return no groups. That is \
a normal answer for a young folder; documents will accumulate and you will be asked \
again.\
"""

_REVIEW_SYSTEM = f"""\
You are a librarian re-examining a folder you divided earlier. You are told what \
distinction it was divided along and how many documents there were at the time. \
There are more now.

**Your default answer is that the existing division still holds.** It was made by \
someone looking at the same archive, and moving documents has a cost paid by every \
person who had learned where things were. Overturn it only if you can say what is \
actually wrong -- a group that no longer means anything, a distinction that has \
turned out to cut across the real one, documents that have gathered and clearly \
belong together elsewhere.

{_SHARED_RULES}

`holds` is true when the division still stands. Then return no groups; a note about \
what changed is still welcome.\
"""

_DIVIDE_USER = """\
FOLDER: {path}
{purpose}
DOCUMENTS SITTING DIRECTLY HERE ({count}):
{documents}
{children}\
"""

_REVIEW_USER = """\
FOLDER: {path}
{purpose}
DIVIDED ALONG: {basis}
AT THE TIME THERE WERE {before} DOCUMENTS; THERE ARE NOW {count}.

SUB-FOLDERS:
{children}

DOCUMENTS SITTING DIRECTLY HERE ({loose}):
{documents}\
"""


class Group(BaseModel):
    """One sub-folder a division would create."""

    name: str = Field(description="Folder name, one level. Not a path.")
    note: str = Field(
        description="One line: what belongs here, and how it differs from its siblings."
    )
    document_ids: list[str] = Field(
        default_factory=list, description="The documents that go into this group."
    )


class Division(BaseModel):
    """Whether to divide a folder, and into what."""

    divide: bool = Field(description="False when there is no distinction worth drawing yet.")
    basis: str = Field(
        default="",
        description="The distinction the groups are drawn along, in a few words. Recorded on the folder and read back when this is reconsidered.",
    )
    groups: list[Group] = Field(default_factory=list, max_length=12)
    rename_to: str | None = Field(
        default=None,
        description="A corrected name for THIS folder when its current one no longer describes what it holds. Null to keep it.",
    )
    reason: str = Field(description="One sentence a person can check against the listing.")


class Review(BaseModel):
    """Whether a division that was already made still holds."""

    holds: bool = Field(description="True when the existing division is still right.")
    reason: str = Field(description="One sentence. If it no longer holds, what is wrong.")
    basis: str = Field(default="", description="The new distinction, when replacing the old one.")
    groups: list[Group] = Field(default_factory=list, max_length=12)
    rename_to: str | None = Field(default=None)


def build_divide(
    *, path: str, purpose: str, documents: list[tuple[str, str]], children: list[tuple[str, str]]
) -> Prompt:
    """Ask whether an undivided folder should be divided.

    Args:
        documents: (document_id, one-line description) for documents directly here.
        children: (name, note) for sub-folders that already exist.
    """
    return Prompt(
        system=_DIVIDE_SYSTEM,
        user=_DIVIDE_USER.format(
            path=path or "(루트)",
            purpose=f"NOTE: {purpose}\n" if purpose else "",
            count=len(documents),
            documents=_render_documents(documents),
            children=_render_children(children),
        ),
    )


def build_review(
    *,
    path: str,
    purpose: str,
    basis: str,
    before: int,
    count: int,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
) -> Prompt:
    """Ask whether an existing division still holds."""
    return Prompt(
        system=_REVIEW_SYSTEM,
        user=_REVIEW_USER.format(
            path=path or "(루트)",
            purpose=f"NOTE: {purpose}\n" if purpose else "",
            basis=basis,
            before=before,
            count=count,
            loose=len(documents),
            documents=_render_documents(documents),
            children=_render_children(children) or "  (none)",
        ),
    )


def _render_documents(documents: list[tuple[str, str]]) -> str:
    if not documents:
        return "  (none)"
    return "\n".join(f"  [{document_id}] {line}" for document_id, line in documents)


def _render_children(children: list[tuple[str, str]]) -> str:
    if not children:
        return ""
    rendered = "\n".join(
        f"  {name}/  — {note}" if note else f"  {name}/" for name, note in children
    )
    return f"EXISTING SUB-FOLDERS:\n{rendered}"
