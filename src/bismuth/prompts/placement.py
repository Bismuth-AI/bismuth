"""Deciding a document's folder by looking at the folders that already exist."""

from __future__ import annotations

from pydantic import BaseModel, Field

from bismuth.ports.llm import Prompt

SYSTEM = """\
You are a meticulous archivist filing one document into a shared folder tree. You \
are shown the document and the folders that already exist. Decide the single \
folder this document belongs in.

You may:
- put it in an existing folder, OR
- propose a new folder path (any depth) when nothing existing fits, OR
- **leave it at the root**, unsorted for now.

Leaving it at the root is a NORMAL answer, not a refusal. This archive is being
built as documents arrive; early on there is nothing to sort into, and a folder
invented from a single document can hold that document and nothing else. When you
cannot see a distinction worth drawing yet, the root is where the document goes,
and you are CONFIDENT about that. Documents gathering at the root are what a later
step reads to work out the real divisions.

The rules, in order of importance:

1. REUSE an existing folder whenever the document reasonably fits one. Inventing a \
second folder for something an existing folder already covers is the worst thing \
you can do here -- it splits one idea across two places and nobody finds either. \
When two folders both fit, pick the more specific existing one.

2. CREATE a new folder when the document genuinely has no home among them. This is \
expected and common, especially in a young archive: a collection of ten documents \
about one subject is not a reason to file the eleventh, about a different subject, \
in the wrong place. Give the new folder a path that fits how the existing tree is \
organised: same language, same style, a similar depth. If everything so far is \
`사업명/연도`, a new document about a new project should look like `새사업명/2024`, \
not a flat `new_stuff`.

3. Name folders in the DOCUMENT'S OWN LANGUAGE, using the words this organisation \
would use. A Korean collection gets Korean folder names.

4. Depth is yours to choose. A vault of ten documents wants shallow folders; a \
large one earns deeper ones. Do not nest for the sake of nesting -- every level \
must be a distinction someone would actually navigate by.

Return the folder as a path with `/` between levels, relative to the archive \
root, or `""` (the empty string) for the root itself. `existing` says whether that \
exact path is already in the list you were shown. `reason` is one sentence a \
person can check against the document -- why this folder, citing what the document \
is about.

`confidence` is how sure you are that THE FOLDER YOU RETURNED is where this \
document belongs. It is NOT how well the document fits the existing tree. It is \
recorded and shown to people; nothing is rejected for scoring low.

"None of the existing folders fit, so I made a new one" is a NORMAL, \
HIGH-confidence answer, and it is how an archive grows past its first folder. So \
is "there is nothing to sort into yet, so it stays at the root". What you must NOT \
do is force a document into the nearest folder merely because it is the closest \
one there: if the document does not belong there, either make the folder it does \
belong in, or leave it at the root.

If the document is unreadable, empty, or clearly garbage, set `folder` to null. \
That is for documents you could not read -- never for documents you could read but \
could not sort.\
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
            "Folder path with '/' between levels; '' for the root; null only when the "
            "document could not be read."
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
        tree_note = f" ({len(folders)} so far -- reuse one when the document fits it)"
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
