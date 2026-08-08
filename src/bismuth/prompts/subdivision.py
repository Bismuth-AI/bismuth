"""Asking what has grown here, and who belongs to it.

The shape of each question is the point, and the shape is not "divide this".

**Nothing is ever partitioned.** A partition has to account for every document, and a
pile of unrelated documents cannot be accounted for -- so the leftovers get a name, and
that name is "everything else". Measured three runs running: `그 밖의 무관한 학술 논문`,
`그 밖의 주제`, `기타 주제`, the last of them while the prompt explicitly forbade those
words. Forbidding the name does not work, because the partition is what demands it.

So instead one class is drawn out at a time (docs/spec/subdivision.md 2, "새 범주 추가").
*Emerging* names a single class and nothing else; *Members* says who belongs to that one
class. Neither reply has a slot the leftovers could go in, and the leftovers stay in the
folder they are already in, which is what SPEC.md 3.4 says should happen to them.

Asking this repeatedly is safe in a way that asking "how would you divide this" is not:
it can only add a sibling, never move a document from one existing folder to another, so
there is nothing for it to oscillate between.

*Still right?* is what an already-divided folder is asked once the evidence has doubled,
and it is the only question that may redraw a boundary.

**The reasoning field comes before the verdict in every schema here, and that ordering
is load-bearing.** Constrained decoding fills fields in schema order, so whichever comes
first is answered with nothing behind it. Measured: 300 documents, 300 times asked, 300
times `emerged: false` -- while the very same replies filled in the axis (`법령의 종류`),
the class (`법률`) and a paragraph explaining that it covered half the archive. The model
had the right answer and had already committed to the wrong one, because the verdict was
the first token it was allowed to write. This is the same failure as asking whether and
how in one call, arriving from the other direction.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from bismuth.ports.llm import Prompt

_SIGNS = """\
Think of folders as SIGNS, not as groups.

Someone is looking for a document. Right now they see the list of documents in this \
folder. If it is divided they see a few folder names INSTEAD, and must choose one \
before they see any document at all. A division is worth making when those names let \
them ignore most of the collection, and it is worth nothing otherwise.

A division fails when:

- **There are nearly as many signs as documents.** Reading nine names to reach nine \
documents is worse than reading nine documents.
- **A sign points at one document.** A sign in front of a single book is not a sign, \
it is the book with a step in front of it.
- **Most documents fit no sign.** Then the reader is back to scanning, and the \
distinction drawn is not the one this collection is organised by.

A sign names a CLASS -- something you expect more of -- never one document's subject. \
If the only honest name for a group is the title of the document inside it, that group \
should not exist.

Signs and notes in the DOCUMENTS' OWN LANGUAGE.\
"""

_EMERGING_SYSTEM = f"""\
These documents have not been sorted, and this folder has no sub-folders yet. You are \
deciding TWO things, and the first one outlives this answer.

**First, the AXIS.** Every sub-folder this folder ever gets will be one answer to a \
single question, and you are choosing that question now. `소관 분야`, `법령의 종류`, \
`사업 연도` are axes; they are the thing the shelves tell apart. You will be held to it: \
later classes must be answers to the same question, so pick one that MORE of these \
documents can answer, not one that fits the document in front of you.

**Then, the first CLASS on it** -- the one value of that axis that has gathered enough \
documents to be worth a shelf.

{_SIGNS}

**You are not dividing this folder and you are not accounting for every document.** \
Whatever does not belong to the class you name stays exactly where it is. Most of these \
documents staying put is the normal outcome, not a failure.

If two classes have grown, name the thicker; you will be asked again and the other can \
come out then.

`emerged` is false when nothing has gathered yet -- common in a young archive, and the \
right answer more often than not. A collection whose documents are each about something \
different is a list, and a list is best left as a list. Say so.\
"""

_EMERGING_ALONG_SYSTEM = f"""\
This folder is already divided, and it is divided ALONG ONE AXIS. You are told what that \
axis is and which answers to it already have shelves. You are deciding one thing: \
whether ANOTHER answer to that same question has gathered enough documents here.

**The axis is not yours to change.** A class on a different axis would put two kinds of \
distinction side by side, and then no name here rules anything out -- a reader has to \
open all of them, which is the cost the folders exist to avoid. If what has gathered is \
real but belongs to a different question, the answer is no: it stays in this folder, and \
a later look at a different level can take it.

{_SIGNS}

Whatever you do not name stays exactly where it is, and most of it staying is normal.

`emerged` is false when nothing new has gathered along this axis. That is the common \
answer and the safe one. Leave `axis` empty; this folder already has one.\
"""

_MEMBERS_SYSTEM = f"""\
A shelf has been decided on and named. Say which of these documents go on it.

{_SIGNS}

Only the ones that genuinely belong under this sign. Everything else stays in the folder \
it is in now -- you are not placing those and you are not being asked about them. \
Leaving most of the list unclaimed is normal and expected.

A document belongs here when someone looking for it would read this sign and stop. If \
you find yourself reasoning that it fits better here than anywhere else, it does not \
belong: there is no anywhere else, there is only this shelf and the folder it is \
already in.\
"""

_REVIEW_SYSTEM = f"""\
You are re-examining a folder you divided earlier. You are told what distinction it \
was divided along and how many documents there were at the time. There are more now.

**Your default answer is that the existing division still holds.** It was made by \
someone looking at the same archive, and moving documents has a cost paid by every \
person who had learned where things were. Overturn it only if you can say what is \
actually wrong -- a sign that no longer means anything, a distinction that has turned \
out to cut across the real one, documents that have gathered and clearly belong \
together elsewhere.

{_SIGNS}

`holds` is true when the division still stands. Then return no groups.\
"""

_LISTING = """\
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


class Emerging(BaseModel):
    """One class that has grown thick enough to leave the pile.

    One name, deliberately. A reply that could carry several would be a partition, and a
    partition of a heterogeneous pile always needs somewhere to put the remainder.

    **``reason`` comes first, and the order is the point** -- see the module docstring.
    """

    reason: str = Field(
        description=(
            "Work it out here, before answering. What classes are in this folder, which "
            "is thickest, and roughly how many of these documents it takes."
        )
    )
    emerged: bool = Field(description="False when no single class has gathered yet.")
    axis: str = Field(
        default="",
        description=(
            "What the sub-folders here tell apart, in a few words -- the question every "
            "one of them is an answer to. Asked only the first time; after that the "
            "folder already has one and you are held to it."
        ),
    )
    name: str = Field(default="", description="Folder name for that class, one level. Not a path.")
    note: str = Field(
        default="",
        description="One line: what belongs here, and how it differs from the folders beside it.",
    )


class Members(BaseModel):
    """Which documents go under one named sign. The rest are not asked about."""

    reason: str = Field(
        description="One sentence a person can check against the listing. Written first."
    )
    document_ids: list[str] = Field(
        default_factory=list,
        description="Only the documents that belong under this sign. The rest stay where they are.",
    )


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
    """The signs to put up, once it has been decided there should be some."""

    reason: str = Field(
        description="One sentence a person can check against the listing. Written first."
    )
    divide: bool = Field(default=True, description="False if, on writing them out, none work.")
    basis: str = Field(
        default="",
        description=(
            "The distinction the signs follow, in a few words. Recorded on the folder and "
            "read back when this is reconsidered."
        ),
    )
    groups: list[Group] = Field(default_factory=list, max_length=12)
    rename_to: str | None = Field(
        default=None,
        description=(
            "A corrected name for THIS folder when its current one no longer describes "
            "what it holds. Null to keep it."
        ),
    )


class Review(BaseModel):
    """Whether a division that was already made still holds."""

    reason: str = Field(
        description=(
            "Work it out here, before answering: what the division was for, and whether "
            "the documents that have arrived since still fall under it."
        )
    )
    holds: bool = Field(description="True when the existing division is still right.")
    basis: str = Field(default="", description="The new distinction, when replacing the old one.")
    groups: list[Group] = Field(default_factory=list, max_length=12)
    rename_to: str | None = Field(default=None)


def build_emerging(
    *,
    path: str,
    purpose: str,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
    axis: str = "",
) -> Prompt:
    """Step one: has any one class grown thick enough to come out?

    With an ``axis``, the folder has been divided before and the question narrows to
    "another answer to the same question?". Without one, the axis is chosen here and
    every sub-folder this folder ever gets is held to it.
    """
    user = _listing(path, purpose, documents, children)
    if not axis:
        return Prompt(system=_EMERGING_SYSTEM, user=user)
    return Prompt(system=_EMERGING_ALONG_SYSTEM, user=f"{user}\n\nTHE AXIS HERE: {axis}")


def build_members(
    *,
    path: str,
    purpose: str,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
    name: str,
    note: str,
) -> Prompt:
    """Step two: who goes on that shelf. Only reached when step one named one."""
    user = _listing(path, purpose, documents, children)
    return Prompt(system=_MEMBERS_SYSTEM, user=f"{user}\n\nTHE SHELF: {name} — {note}")


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


def _listing(
    path: str, purpose: str, documents: list[tuple[str, str]], children: list[tuple[str, str]]
) -> str:
    return _LISTING.format(
        path=path or "(루트)",
        purpose=f"NOTE: {purpose}\n" if purpose else "",
        count=len(documents),
        documents=_render_documents(documents),
        children=_render_children(children),
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
