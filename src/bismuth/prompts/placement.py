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
- propose a new folder path (any depth) when nothing existing fits.

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
root. `existing` says whether that exact path is already in the list you were \
shown. `reason` is one sentence a person can check against the document -- why \
this folder, citing what the document is about.

`confidence` is how sure you are that THE FOLDER YOU RETURNED is where this \
document belongs. It is NOT how well the document fits the existing tree.

A document about a subject the archive has never held yet still has an obvious \
home: a new folder for it. If you know what that folder should be called, you are \
confident -- say so. "None of the existing folders fit, so I made a new one" is a \
NORMAL, HIGH-confidence answer, and it is how an archive grows past its first \
folder. Reserve low confidence for documents you genuinely cannot read or cannot \
tell apart from two equally good homes.

If the document is unreadable, empty, or clearly garbage, set `folder` to null.\
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

_EMPTY_TREE = "(none yet -- this is the first document, so you are creating the first folder)"


class PlacementDecision(BaseModel):
    """Where the model says the document goes."""

    folder: str | None = Field(
        description="Folder path with '/' between levels, or null if the document is unfilable."
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
