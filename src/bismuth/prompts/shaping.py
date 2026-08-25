"""Two-stage batch filing prompts: choose a neighbourhood, then shape it."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from bismuth.ports.llm import Prompt
from bismuth.prompts.simple import Folder, _size, _tree

_NEAREST = """\
Choose only which part of the folder tree each document is nearest to. The next step
will decide whether to file it, create a folder, or leave it at the root. This step only
points to a neighbourhood.

For every document, choose the single nearest existing folder.

"Nearest" does not mean "an exact fit." Choose a folder whenever it covers the same
branch of the collection. The next step will inspect that folder's contents and decide
whether to file directly, create a child, build a parent over related folders, or refuse
the placement. Do not answer `NONE` merely because the fit is imperfect. Use `NONE` only
when the document belongs to a genuinely different branch from every existing folder.

Judge the branch on independent dimensions: central subject, purpose or activity, and the
organizing scope stated by the folder. Shared vocabulary is not evidence of membership by
itself. A candidate is near only when the document satisfies every constraint expressed by
the folder purpose. If one dimension conflicts, treat it as a different branch and answer
`NONE` so the next step can create an honest top-level subject.

The current tree is evidence about existing contents, not a closed taxonomy of everything
the collection may accept. Never infer a collection-wide restriction from the dominant
kind of existing document. A genuinely new branch is allowed and should remain unassigned
here rather than being forced into the least-wrong existing branch.

Folders are shown one per line:
  <path>/ (<all documents below>, <direct documents>, <child folders>) — <purpose>

A folder without children is shown as `(<n> documents)`. The first number is everything
below the folder. A large, divided folder may hold few documents directly; that means it
is organized, not empty.

Documents are shown one per line:
  [D<number>] <document title> | <document type> | <topics> | <summary>

Return exactly one line per document and nothing else:
D1: <folder path or NONE>
D2: <folder path or NONE>\
"""

_SHAPING = """\
Maintain a folder tree for an agent that can navigate only with `ls`. Folder names are
the agent's only clues.

There are two kinds of handles in the input. `[F1]` identifies a folder and `[D1]`
identifies a document. In the answer, always use an `F` handle where a folder is required
and a `D` handle where a document is required. Do not copy names in place of handles. If
you put a document title where a folder handle belongs, no operation will take place.

Being routed to a neighbourhood does not mean the document belongs there. The previous
step chose the nearest place, not necessarily the correct place. Test it first:

  Would someone reading the folder's purpose expect this document to be there?

If not, do not file it there. Use `INSIDE` for another existing folder when that folder
fits, or `ROOT` when none does. A wrongly filed document may never be found; leaving it
at the root is better than forcing it into a misleading folder.

Treat a folder's purpose as a contract. Test it on independent dimensions: central
subject, purpose or activity, audience, and organizing scope. Eligibility is conjunctive:
every applicable constraint must pass. One conflict vetoes that destination completely;
topical similarity cannot compensate for it. If a reader relying only on the folder name
and purpose would not predict the document, use another existing folder or a new top-level
subject.

The existing tree is not a closed taxonomy. Its dominant document kind does not restrict
what future top-level branches may contain. Do not use `ROOT` merely because the arriving
document introduces a kind of knowledge not represented by the current branches.

At each neighbourhood, choose among four operations. Make the decision from the current
tree and the arriving document, even when only one document is arriving.

1. File directly with `INSIDE` only when the document belongs to the folder and the
   folder is still small. The input states how many documents the folder holds directly.
   If it already holds 25 or more direct documents, do not use `INSIDE`; create a useful
   child with `BELOW`. Opening a folder over that limit should reveal folder names, not a
   long document list.
2. Create a child with `BELOW` when the arriving document belongs to a distinct,
   reusable subject inside the folder. This includes a single arriving document when the
   subject is a durable category that future documents could also use. Prefer `BELOW`
   over direct filing when the existing folder is broad, direct placement would be
   misleading, or the folder already holds 25 direct documents.
3. Rename with `RENAME` when the folder's current name no longer describes its contents.
4. Create a new top-level folder with `CREATE` when no existing folder fits and a durable
   subject can be inferred. A single arriving document is enough to establish such a
   category. Include a `SIGN` for every folder you create.

Before using `ROOT`, derive the smallest stable abstraction that describes the document
and could accept genuinely similar future documents. Preserve every distinguishing
dimension needed to keep it separate from existing branches, while dropping incidental
metadata and document-specific names. Prefer `CREATE` when that abstraction passes both
tests: a different future document could belong there, and documents from at least one
existing branch would clearly not belong there. Use `ROOT` only when no candidate can
pass both tests.

Construct the name systematically. Begin with the central subject. Compare it with the
nearest vetoed folders; when the subject alone would blur a scope difference, add the
document's stable purpose or activity as a qualifier. Use the shortest label that preserves
all dimensions responsible for separating the new branch. Never use format, date, or one
document's proper name as that qualifier.

Several operations may be returned in one answer. Do not group existing sibling folders
under a parent here; that is asked separately. A proposed new child is checked separately
before it is applied. Similarity is not containment: a child belongs below a parent only
when the parent's stated scope actually includes it.

When naming a folder, describe what its documents are about, not what kind of documents
they are. Format, genre, date, publisher, creator, language, and hierarchy are metadata,
not subjects. Distinguish a folder that currently contains one document from a folder
whose scope can contain only that document: the former is allowed, the latter is an
index and is forbidden. Do not copy or lightly shorten a document title, filename, work
or series title, exact document family, issuer, date, or identifier into a folder name.
Do not join title fragments with `and` merely to cover the current item. Name the
reusable subject instead. Before choosing a name, ask both: can you name a document in
this collection for which the name would be false, and could a different future document
also belong there? If the second answer is no, use `ROOT`. Do not use vague names such as
`miscellaneous`, `general`, `related`, or `other`.

Return only these tagged lines, repeated as needed:
INSIDE: F2 | D1, D3
BELOW: F2 | <new child name> | D2, D5
RENAME: F2 | <new name>
CREATE: <new folder name> | D4, D6
ROOT: D7, D9
SIGN: <new name> | <one sentence describing what it contains>

Perform all comparisons silently. Return the tagged lines immediately, without analysis,
explanation, headings, bullets, or Markdown. Prose outside the tagged lines is invalid.

`ROOT` means that no existing folder fits and no honest reusable subject can yet be
inferred. It is a valid answer, but do not use it merely because only one document is
arriving.\
"""

_PARENT_SCOPE = """\
Decide whether one proposed child belongs under its proposed parent. This is a strict
containment check, not a similarity check.

Return `KEEP` only when an agent seeing the parent name and purpose would expect the child
before opening it. Sharing a broad domain is insufficient; every constraint expressed by
the parent scope must include the child.

When the child and the parent's current contents together reveal an honest broader
subject, return `PROMOTE` with that broader parent name and a purpose. Otherwise return
`SIBLING`; the child will be created beside the parent instead of below it.

Return exactly one of:
KEEP
SIBLING
PROMOTE: <broader parent name> | <one sentence describing its scope>\
"""

_FINAL_SCOPE_GATE = """\
FINAL ELIGIBILITY GATE — apply this after reading the documents above:
- For every existing destination, compare all constraints in its stated purpose with the
  document on subject, purpose or activity, audience, and organizing scope.
- Eligibility requires all applicable dimensions to pass. One conflict makes that
  destination invalid regardless of similarity on other dimensions.
- The existing tree is not a closed taxonomy. If every existing destination is invalid,
  derive a reusable new subject; do not choose the least-wrong existing folder.
- A bare central subject is insufficient when it could also describe a vetoed branch.
  Add the stable purpose or activity responsible for the veto, using the shortest label
  that preserves that distinction in the folder name itself; the `SIGN` cannot repair an
  underspecified name.
- Separate the enduring object or work described by the document from the circumstances
  in which it was authored, submitted, reviewed, announced, or stored. Name the former.
  Replace proper nouns, dates, sponsors, and surrounding occasions mentally; a reusable
  name must still describe the document's substantive body after those changes.
- Treat the summary as the account of the substantive body; title, type, and topics are
  retrieval cues and cannot overrule it. Identify which object or work owns most of the
  summary's goals, actions, functions, and outcomes, and center the new name on that
  primary referent rather than on the frame through which the document was produced.
- Apply a role test when a focal work participates in, is submitted to, is funded by, or
  is reviewed within a surrounding structure. That relation does not make the surrounding
  structure the primary subject. Center it only when the substantive body describes that
  structure's own rules, operation, or aggregate outcomes. If the context remains useful
  as a qualifier, the name must still state the focal work's reusable class.
- When the substantive body records a bounded undertaking with goals, execution, and
  outcomes, preserve its enduring work or activity class in the folder name. Do not reduce
  it to a thematic domain or name it after the reporting genre or surrounding occasion.
- Check the proposed name against its `SIGN`: every scope-bearing distinction needed to
  exclude a vetoed branch must appear in the name itself. Derive the `SIGN` only from the
  arriving documents and plausible siblings, never from traits of the vetoed folders.
- Before returning `ROOT`, verify that no stable abstraction can accept another future
  document while excluding at least one existing branch.
Return tagged lines only.\
"""

_GROUPING = """\
You see only folders, never documents. Too many folders stand at this level, forcing a
reader to scan the whole list. A list of fifty names is an index, not a useful menu.

Find folders from the same branch, name the subject that covers them, and group them
under it.

Rules:
1. A group must contain at least two folders. A parent over one child adds depth and
   filters nothing.
2. Refer to child folders only by their exact `F` handles. Count them carefully.
3. A new parent must cover every selected child and no more. A broad parent swallows
   future documents; a narrow parent misdescribes some children.
4. Leave unrelated folders in place. Not every folder must be grouped.
5. An existing broader folder may be the parent. Use its `F` handle as the parent and do
   not include it among its own children; a folder cannot be moved under itself. When one
   large folder stands beside several folders holding only two or three documents, some
   of those small folders often belong inside the large one. Apply the same quality test
   to an existing parent as to a new one. A name taken from one document is that
   document's place, not a parent covering several branches; create a broader name that
   covers it together with its siblings instead.
6. Do not create a parent whose name is effectively the same as one child. A parent must
   cover the children together and must not be interchangeable with any single child.
7. Add the document counts in parentheses before grouping. If the new parent would hold
   more than half of this entire level, it is the whole collection rather than a useful
   category. A reader cannot narrow anything under such a name, and most future documents
   will also be sent there. Check whether two distinct branches are being forced together.
   If the name needs `and` to make the subjects cohere, they usually should remain separate.

Names describe what the contents are about, not their format, type, date, creator, or
publisher. Grouping by the organization that produced documents mixes every subject that
organization handles and gives the reader no useful clue.

Apply two tests to every proposed name:
1. Can you name a document in this collection for which the name would be false? If not,
   the name excludes nothing.
2. If the child names were hidden, would the parent name make every child expected? Drop
   any child that would be surprising.

Do not use `miscellaneous`, `general`, `related`, or `other`.

Return `NONE` and nothing else when no useful group exists. Otherwise return only:
GROUP: <new name or existing F handle> | F1, F3, F7
SIGN: <new name> | <one sentence describing what it contains>\
"""


def build_nearest(
    *,
    folders: list[Folder],
    documents: list[tuple[str, str]],
    language: str = "",
) -> Prompt:
    """Which part of the tree each document is near. One call for the batch."""
    say = f"The documents are written in `{language}`.\n\n" if language else ""
    listed = "\n".join(f"  [{handle}] {line}" for handle, line in documents)
    return Prompt(
        system=_NEAREST,
        user=(
            f"{say}CURRENT TREE:\n{_tree(folders)}\n\n"
            f"DOCUMENTS TO ROUTE ({len(documents)}):\n{listed}"
        ),
    )


@dataclass(frozen=True, slots=True)
class Place:
    """One neighbourhood as the second question is shown it."""

    folder: PurePosixPath
    note: str
    holding: list[str]
    """What is already in it, one line per document -- the first few, not all of them."""

    held: int
    """Total direct documents, including entries omitted from the prompt preview."""

    children: list[str]
    arriving: list[tuple[str, str]]
    """The documents this batch sent here."""


def build_shaping(
    *,
    folders: list[Folder],
    places: list[Place],
    homeless: list[tuple[str, str]],
    language: str = "",
) -> Prompt:
    """Choose how to file documents within each routed neighbourhood."""
    handles = {str(folder.path): f"F{index}" for index, folder in enumerate(folders, start=1)}
    listing = (
        "\n".join(
            f"  [{handles[str(folder.path)]}] {folder.path}/  {_size(folder)}"
            + (f" — {folder.note}" if folder.note else "")
            for folder in folders
        )
        or "  (no folders yet)"
    )
    blocks = [f"CURRENT FOLDERS:\n{listing}"]
    for place in places:
        held = "\n".join(f"    - {line}" for line in place.holding) or "    (none)"
        if place.held > len(place.holding):
            held += f"\n    … {place.held - len(place.holding)} more not shown"
        kids = ", ".join(place.children) or "none"
        coming = "\n".join(f"    [{handle}] {line}" for handle, line in place.arriving)
        mine = handles.get(str(place.folder), "?")
        blocks.append(
            f"■ NEIGHBOURHOOD [{mine}] {place.folder}/ — {place.held} direct document(s), "
            f"{len(place.children)} child folder(s)"
            + (f"\n  ROUTING NOTE: {place.note}" if place.note else "")
            + f"\n  DOCUMENTS ALREADY HERE:\n{held}"
            + f"\n  CHILD FOLDERS: {kids}"
            + f"\n  ARRIVING DOCUMENTS:\n{coming}"
        )
    if homeless:
        listed = "\n".join(f"    [{handle}] {line}" for handle, line in homeless)
        blocks.append(
            "■ NEIGHBOURHOOD: ROOT — documents with no nearby folder.\n"
            "  Reconsider every existing folder by its purpose. Use INSIDE only when a "
            "reader would expect the document there, and never when that folder already "
            "has 25 or more direct documents. If no folder fits, use CREATE for a durable "
            "subject that future documents could reuse, even when only one document is "
            f"arriving, or ROOT when no such subject can be inferred.\n{listed}"
        )
    say = (
        f"The documents are written in `{language}`. Write folder names and signs in "
        f"`{language}` using their vocabulary.\n\n"
        if language
        else ""
    )
    return Prompt(system=_SHAPING, user=say + "\n\n".join((*blocks, _FINAL_SCOPE_GATE)))


_SETTLING = """\
Make a final pass over one level of folders. Some are broad folders containing several
branches; others are small folders containing a single branch. Ask one question:

  Does this small folder belong inside that broad folder?

Each broad folder includes a purpose describing its contents. Move a small folder under
it only when a reader opening that broad folder would expect to find the small folder
there. Small single-branch folders left beside broad categories can overwhelm the useful
top-level choices.

Leave a folder in place when no broader folder fits. A genuinely distinct branch belongs
at this level, and forcing it elsewhere makes it hard to find.

You may also group at least two small folders under a new reusable subject when no
existing broad folder covers them.

Refer to every folder by its exact `F` handle. Return `NONE` and nothing else when nothing
should move. Otherwise return only the following lines. When moving into an existing
broad folder, use that folder's `F` handle as the parent:
GROUP: F6 | F2, F9
GROUP: <new name> | F1, F8
SIGN: <new name> | <one sentence describing what it contains>\
"""


def build_grouping(*, folders: list[Folder], settling: bool = False, language: str = "") -> Prompt:
    """Find sibling folders that benefit from one reusable parent."""
    listing = "\n".join(
        f"  [F{index}] {folder.path.name}/  {_size(folder)}"
        + (f" — {folder.note}" if folder.note else "")
        for index, folder in enumerate(folders, start=1)
    )
    where = folders[0].path.parent if folders and folders[0].path.parts[:-1] else None
    say = (
        f"This archive is written in `{language}`. Write names in `{language}`.\n\n"
        if language
        else ""
    )
    return Prompt(
        system=_SETTLING if settling else _GROUPING,
        user=(
            f"{say}{len(folders)} FOLDERS AT "
            f"{'`' + str(where) + '/`' if where else 'THE ROOT'}:\n\n{listing}"
        ),
    )


def parse_grouping(
    text: str, folders: list[Folder]
) -> tuple[list[tuple[str, list[str]]], dict[str, str]]:
    """``[(new parent, folders under it)]`` and the sentences for the new names."""
    known = {f"F{index}": str(folder.path) for index, folder in enumerate(folders, start=1)}
    groups: list[tuple[str, list[str]]] = []
    signs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        tag, separator, value = line.partition(":")
        tag, value = tag.strip(), value.strip()
        if not separator or not value:
            continue
        left, _, right = value.partition("|")
        # The parent may be a folder that already stands, named by its handle: a good name
        # earning more of the tree is the same operation as a new name being invented.
        name, right = known.get(left.strip().upper(), _named(left)), right.strip()
        if not name or not right:
            continue
        if tag.upper() == "GROUP":
            under = [
                found for part in right.split(",") if (found := known.get(part.strip().upper()))
            ]
            # One is allowed here; whether it is enough depends on the parent existing,
            # which only the caller knows.
            if under:
                groups.append((name, under))
        elif tag.upper() == "SIGN":
            signs[name] = right
    return groups, signs


@dataclass(frozen=True, slots=True)
class Shaped:
    """What the second question asked for."""

    inside: dict[str, list[str]] = field(default_factory=dict)
    """``folder -> handles`` put straight in."""

    below: dict[str, list[str]] = field(default_factory=dict)
    """``new sub-folder path -> handles``."""

    checked_below: set[str] = field(default_factory=set)
    """New sub-folder paths approved by the focused parent-scope check."""

    made: dict[str, list[str]] = field(default_factory=dict)
    """``new folder path -> handles``, for documents that had no near place."""

    renamed: list[tuple[str, str]] = field(default_factory=list)
    loose: list[str] = field(default_factory=list)
    signs: dict[str, str] = field(default_factory=dict)


def parse_nearest(text: str) -> dict[str, str]:
    """Parse nearest-folder answers; NONE and ROOT remain unassigned."""
    near: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        handle, separator, value = line.partition(":")
        handle, value = handle.strip(), value.strip().strip("/")
        if not separator or not handle.upper().startswith("D") or not handle[1:].isdigit():
            continue
        if value and value.upper() not in ("NONE", "-", "ROOT"):
            near[handle.upper()] = value
    return near


def parse_shaping(text: str, folders: list[Folder]) -> Shaped:
    """The four moves, plus the two answers for documents that had no place.

    ``folders`` is the same list the question was built from, in the same order: that is
    what an ``F`` handle means. A folder named in prose instead is accepted when it matches
    one exactly, and dropped otherwise -- a name that resolves to nothing would silently
    become a new top-level folder, which is the failure this rewrite exists to stop.
    """
    known = {f"F{index}": str(folder.path) for index, folder in enumerate(folders, start=1)}
    paths = {str(folder.path) for folder in folders}

    def standing(value: str) -> str:
        """A folder that exists, by handle or by exact name. Empty when it is neither."""
        cleaned = value.strip().strip("/")
        if (found := known.get(cleaned.upper())) is not None:
            return found
        return cleaned if cleaned in paths else ""

    shaped = Shaped()
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        tag, separator, value = line.partition(":")
        tag, value = tag.strip(), value.strip()
        if not separator or not value:
            continue
        parts = [part.strip() for part in value.split("|")]
        left, right = parts[0].strip("/"), parts[1] if len(parts) > 1 else ""
        upper = tag.upper()
        if upper == "INSIDE" and (where := standing(left)) and right:
            shaped.inside.setdefault(where, []).extend(_handles(right))
        elif upper == "BELOW" and (where := standing(left)) and len(parts) > 2:
            below = f"{where}/{_named(parts[1])}"
            shaped.below.setdefault(below, []).extend(_handles(parts[2]))
        elif upper == "CREATE" and (name := _named(left)) and right:
            shaped.made.setdefault(name, []).extend(_handles(right))
        elif upper == "RENAME" and (where := standing(left)) and (name := _named(right)):
            shaped.renamed.append((where, name))
        elif upper == "ROOT":
            shaped.loose.extend(_handles(value))
        elif tag.upper() == "SIGN" and left and right:
            shaped.signs[standing(left) or _named(left)] = right
    return shaped


def build_parent_scope(*, parent: Folder, child: str, documents: list[str]) -> Prompt:
    """Check a proposed hierarchy without making the filing question more complicated."""
    listed = "\n".join(f"  - {line}" for line in documents)
    return Prompt(
        system=_PARENT_SCOPE,
        user=(
            f"PARENT: {parent.path}/\n"
            f"PURPOSE: {parent.note or parent.path.name}\n"
            f"PROPOSED CHILD: {child}/\n"
            f"DOCUMENTS GOING THERE:\n{listed}"
        ),
    )


def parse_parent_scope(text: str) -> tuple[str, str, str]:
    """Return ``(decision, promoted name, purpose)``."""
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if line.upper() == "KEEP":
            return "keep", "", ""
        if line.upper() == "SIBLING":
            return "sibling", "", ""
        tag, separator, value = line.partition(":")
        if separator and tag.strip().upper() == "PROMOTE":
            name, pipe, purpose = value.partition("|")
            if pipe and (named := _named(name)) and purpose.strip():
                return "promote", named, purpose.strip()
    return "sibling", "", ""


def _named(value: str) -> str:
    """A name the reply invented. Handles are not names -- ``F2`` as a new folder is a slip."""
    cleaned = value.strip().strip("/").strip()
    if cleaned.upper().startswith(("F", "D")) and cleaned[1:].isdigit():
        return ""
    return cleaned


def _handles(value: str) -> list[str]:
    """``D1, D3`` -- and forgiving about how they are separated."""
    out = []
    for part in value.replace("/", ",").replace("·", ",").split(","):
        handle = part.strip().upper().strip(".")
        if handle.startswith("D") and handle[1:].isdigit():
            out.append(handle)
    return out
