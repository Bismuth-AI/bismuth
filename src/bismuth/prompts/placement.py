"""Choosing a document's folder from the folders that already exist.

**Nothing here invents a folder.** Placement answers "where in the tree as it stands",
and a tree that does not have a place for this document is answering "the root" -- which
is what SPEC.md 3.4 says the root is for. Categories come from subdivision, the operation
that sees several documents before it names anything.

Measured on 300 legal documents: placement invented twenty folders, and every one of the
twenty was named after a single document's title (`제조물 책임법`, `국제우편규정`). Three
were created while the vault held two, three and six folders, which is exactly when the
prompt says to use the root. The rest were the model correctly obeying "make a new folder
that fits how the existing tree is organised" -- the tree was by then organised as a list
of statute names, so it kept adding statute names. A first mistake became the precedent
for every one after it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from bismuth.ports.llm import Prompt

SYSTEM = """\
You are a meticulous archivist filing one document into a shared folder tree. You \
are shown the document and the folders that exist. Choose the one it belongs in.

There are exactly two answers:

- **one of the folders you were shown**, copied exactly, OR
- **the root** (`""`), where it waits.

**You cannot make a new folder.** Not a new name, not a new path, not a deeper level \
under an existing one. If nothing shown fits, the answer is the root, and that is a \
CONFIDENT answer rather than a refusal.

The root is not a failure state, a holding pen, or an admission you could not decide. \
It is where documents wait for a distinction to become visible, and it is read later by \
a step that sees many documents at once and draws the real divisions from them. That \
step is the only thing that makes folders here. It can see what you cannot from one \
document: whether a class has enough behind it to deserve a shelf.

The rules, in order of importance:

1. REUSE the folder the document reasonably fits. When two fit, pick the more \
specific one.

2. If none fits, answer the root. Do not force a document into the nearest folder \
merely because it is the closest one there -- a document filed where it does not \
belong is worse than one waiting at the root, because nobody will look for it where \
it ended up.

3. The root is also the answer early on, when there is little or nothing to sort into. \
An archive of ten documents with no folders is not unfinished; it is what ten \
documents look like.

Return the folder EXACTLY as it appears in the list, or `""` for the root. A path that \
is not in the list is read as the root, so there is nothing to gain by inventing one. \
`reason` is one sentence a person can check against the document -- why this folder, \
citing what the document is about.

`confidence` is how sure you are that THE FOLDER YOU RETURNED is where this document \
belongs. It is NOT how well the document fits the existing tree, and "the root, because \
nothing here fits yet" can be a 1.0. It is recorded and shown to people; nothing is \
rejected for scoring low.

If the document is unreadable, empty, or clearly garbage, set `folder` to null. That is \
for documents you could not read -- never for documents you could read but could not \
sort.\
"""

_USER = """\
CURRENT FOLDERS{tree_note}:
{tree}

DOCUMENT TO FILE
title: {title}
type: {doc_type}
about: {topics}
summary: {summary}
mentions: {entities}\
"""

_EMPTY_TREE = "(none yet -- nothing has been sorted into folders. The root is where this goes.)"


class PlacementDecision(BaseModel):
    """Where the model says the document goes."""

    folder: str | None = Field(
        description=(
            "One of the folder paths you were shown, copied exactly; '' for the root; "
            "null only when the document could not be read. Anything else is read as "
            "the root."
        )
    )
    existing: bool = Field(
        default=False, description="True if this exact path was in the folders shown to you."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(
        description="One sentence: why this folder, citing what the document is about."
    )


def build(
    *,
    folders: list[tuple[str, str]],
    title: str,
    doc_type: str,
    topics: list[str],
    summary: str,
    entities: list[str],
) -> Prompt:
    """Build the placement prompt.

    Args:
        folders: (path, purpose) pairs for every existing folder.
    """
    if folders:
        tree = "\n".join(
            f"  {path}" + (f"  — {purpose}" if purpose else "") for path, purpose in folders
        )
        tree_note = f" ({len(folders)} -- choose one of these, or the root)"
    else:
        tree, tree_note = _EMPTY_TREE, ""

    return Prompt(
        system=SYSTEM,
        user=_USER.format(
            tree=tree,
            tree_note=tree_note,
            title=title,
            doc_type=doc_type,
            topics=", ".join(topics) or "(none)",
            summary=summary,
            entities=", ".join(entities) or "(none)",
        ),
        # Folder tree repeats across a batch; cache_hint lets providers cache it.
        cache_hint=True,
    )
