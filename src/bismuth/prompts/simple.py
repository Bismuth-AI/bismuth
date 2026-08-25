"""Prompts for reviewing and locally reorganizing a navigable folder tree."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from bismuth.ports.llm import Prompt

_REVIEW = """\
You are looking at a folder tree that an agent navigates with `ls`. Decide whether the
tree is still worth walking and report the result exactly.

Folders are shown one per line:
  <path>/ (<all documents below>, <direct documents>, <child folders>) — <purpose>

A leaf is shown simply as `(<n> documents)`. `direct` is the unorganized pile held by the
folder itself. If that pile is larger than everything organized below it, the children
are doing little useful filtering.

A reader lists a folder, sees the names inside, and chooses one without opening them.
The tree works only when names at each level separate what lies below.

Run all nine checks. Count each one explicitly; do not answer from an overall impression.

CHECK 1 — Does any folder hold more than 25 documents directly? Opening such a folder
shows a document list instead of a short list of useful folder names. Its contents must
be reorganized.

CHECK 2 — Does any folder hold more documents directly than all of its children hold
together? Its children are not filtering most of the contents. A large folder without
children also fails this check.

CHECK 3 — Are documents still loose at the root? Before the collection reaches 50
documents, fewer than 10 may remain there. At 50 documents or more, even one loose root
document must be refiled because the collection has an established tree.

CHECK 4 — Is any top-level folder actually a narrower branch of another top-level folder?
If one purpose describes a branch of another purpose, they are parent and child, not
siblings.

CHECK 5 — Are there more than 20 top-level folders? Depth has a cost, but width is not
unlimited either. Scanning twelve clear names at once is still one decision, while one
meaningless name is worse than ten meaningful names. Do not blur meanings merely to
reduce the count.

CHECK 6 — Does any folder purpose no longer match what it contains? A purpose is written
when the folder is created and does not know what arrives later. Future filing decisions
will rely on it, so a stale purpose must be corrected.

CHECK 7 — Does any folder name fail to describe what lies below it? Names are also based
on the first few documents and can become stale. A folder named after one document but
later containing several branches is actively misleading even when its direct pile is
small. This check still applies when the direct pile is small but the subtree below it is
large.

CHECK 8 — Does any child conflict with its parent's purpose? If a reader of the parent
would not expect that child there, it is misplaced. This is the most expensive tree
error because a reader may never find the child.

CHECK 9 — Do two separate folders describe the same subject? Their names may differ, but
if their purposes cover the same branch, a reader may open one and miss the other half.
Move one under the other or place both under a shared parent.

Do not change anything that passes these checks. A trustworthy organization produces the
same structure when reviewed twice.

Begin the answer with exactly nine check lines. Use `NONE` when a check finds nothing:
CHECK1: <folders over 25 direct documents, with counts, or NONE>
CHECK2: <folders whose direct pile is larger than all children, or NONE>
CHECK3: <number of loose root documents>
CHECK4: <top-level folders that belong under another, or NONE>
CHECK5: <number of top-level folders>
CHECK6: <folders with stale purposes, or NONE>
CHECK7: <folders whose names do not describe their contents, or NONE>
CHECK8: <children that conflict with their parents, or NONE>
CHECK9: <separate folder pairs that cover the same subject, or NONE>

If every check passes — CHECK3 is zero when DOCUMENTS is at least 50, otherwise below 10,
and CHECK5 is at most 20 — append only:
KEEP

Otherwise append every required action. Checks 1, 2, and 3 require `REFILE`; checks 4,
5, 7, 8, and 9 require `MOVE`; check 6 requires `SIGN`:
MOVE: <current path> | <target path>
REFILE: <folder path, or ROOT>
SIGN: <folder path> | <one-sentence purpose matching its current contents>

`MOVE` relocates a whole folder and all of its contents. It can:
- move under another folder: `MOVE: A/B | C/B`;
- rename in place: `MOVE: A/B | A/new-name`;
- move to the root: `MOVE: A/B | B`.

`REFILE` reorganizes documents held directly by the named folder into its children. Name
the folder only; the documents will be shown in the next step. Do not use `REFILE` on an
empty folder whose only problem is its name. `REFILE: ROOT` reorganizes loose root
documents into existing or new folders.\
"""

_REFILE = """\
Reorganize one folder whose documents are piled directly inside it. The reader should
open this folder and see useful child names, not a long document list.

Nothing may leave the current folder. Every answer must name a child inside it or `STAY`.
Even when another part of the tree looks better, do not move a document outside this
folder; local repair must not disturb the whole tree.

Existing children are shown one per line:
  <name>/ (<direct documents>) — <purpose>

Documents to refile are shown one per line:
  [D<number>] <document title> | <document type> | <topics> | <summary>

For each document:

1. Check existing children first. Compare the child's stated purpose with the document's
   subject, not just matching words in the title. A semantic fit is valid even when the
   wording differs.
2. If no existing child fits, compare the arriving documents with one another. Create a
   new child only when at least two documents share a reusable subject. Never create a
   one-document child: it adds a click and filters nothing.
3. If neither applies, answer `STAY`. Leaving a few documents directly in the current
   folder is valid and better than forcing a false category.

Do not put every document into one child. That merely repeats the current folder and
adds a click before showing the same list.

No child should receive more than 25 documents. If a proposed branch would exceed that
size, look for meaningful subdivisions within it. Twenty-five is the largest direct list
a reader should have to scan.

Child names describe what documents are about, not what kind of documents they are.
Format, genre, date, publisher, creator, and hierarchy are metadata and do not divide the
subject. Do not paraphrase the current folder's name: every document already satisfies
that name, so repeating it excludes nothing.

A child named after one document is an index, not a category. A new child must be broad
enough to accept future documents while still excluding documents from other subjects.
Do not use `miscellaneous`, `general`, `related`, or `other`.

Rename the current folder when its name no longer covers everything inside it. This is
the moment when all of its direct contents are visible, so this is the only step able to
judge the name against them. If the name fits only a few documents and not the rest, it
was probably coined from the first arrivals and became stale as other subjects accumulated.
Rename it to cover everything. Leaving a stale name causes future filing to send more
documents to the wrong place.

Return exactly one line per document and no other prose:
D1: <child folder name or STAY>
D2: <child folder name or STAY>

For every newly created child, append:
SIGN: <child folder name> | <one sentence describing what it contains>

When the current folder itself must be renamed, append:
RENAME: <new name for the current folder>\
"""

_ROOT_REFILE = """\
Place every loose root document into the established folder tree. The collection is now
large enough that the root is no longer a valid holding area.

The complete folder tree is shown with full paths. For each document, choose the most
specific existing folder whose purpose honestly covers it. When no existing folder fits,
name a new reusable top-level subject. A new folder may begin with one document; it must
still be broad enough for different future documents.

Evaluate every destination on independent dimensions: central subject, purpose or
activity, and the organizing scope stated by the folder. Shared words are insufficient.
Eligibility is conjunctive: an existing folder fits only when the document satisfies
every applicable constraint expressed by its purpose and a reader relying on that purpose
would predict the document. One conflict vetoes the destination completely; relevance on
another dimension cannot restore it.

The existing tree is evidence about current contents, not a closed taxonomy. Never infer
a collection-wide restriction from its dominant document kind. When an arriving document
introduces a new but reusable branch, that is a reason to create a top-level subject, not
to force it into the nearest existing branch.

When none fits, derive the smallest stable abstraction that preserves the dimensions
which distinguish this document from existing branches while discarding incidental
metadata and document-specific names. Create it only when a different future document
could belong there and documents from at least one existing branch clearly could not.
Prefer that truthful new subject over the nearest but misleading existing folder.

Construct a new name by starting with the central subject. Compare it with every vetoed
nearby folder; if the subject alone would hide the dimension that caused the veto, add the
document's stable purpose or activity as a qualifier. Use the shortest name that preserves
the separation, never its format, date, or document-specific proper name.

`STAY`, `ROOT`, an empty answer, and an overfull destination are invalid. Do not force a
document into a misleading folder merely to use an existing name; create a new subject
instead. No destination may hold more than 25 direct documents.

Return exactly one line per document:
D1: <existing full folder path or new top-level subject>
D2: <existing full folder path or new top-level subject>

For every new subject, append:
SIGN: <new top-level subject> | <one sentence describing what it contains>

`SIGN` is only for a newly named subject; never repeat or rewrite an existing folder's
purpose here. Perform all comparisons silently and return tagged lines immediately. Do not
emit analysis, explanations, headings, bullets, or Markdown.\
"""

_ROOT_FINAL_GATE = """\
FINAL ELIGIBILITY GATE — apply this after reading the documents above:
- An existing destination is valid only when every applicable constraint in its purpose
  passes on subject, purpose or activity, audience, and organizing scope.
- One conflict vetoes that destination regardless of similarity on another dimension.
- The current tree is not a closed taxonomy. If all existing folders are vetoed, create
  the smallest reusable new subject that preserves the dimensions causing separation.
- A bare central subject is invalid when it could also describe a vetoed folder. Add the
  stable purpose or activity that caused the veto, using the shortest name that preserves
  that distinction in the destination name itself; the `SIGN` cannot repair an
  underspecified name.
- Separate the enduring object or work described by the document from the circumstances
  in which it was authored, submitted, reviewed, announced, or stored. Name the former.
  Mentally replace proper nouns, dates, sponsors, and surrounding occasions; a reusable
  destination must still describe the substantive body after those changes.
- Treat the summary as the account of the substantive body; title, type, and topics are
  retrieval cues and cannot overrule it. Identify which object or work owns most of the
  summary's goals, actions, functions, and outcomes, and center the destination on that
  primary referent rather than on the frame through which the document was produced.
- Apply a role test when a focal work participates in, is submitted to, is funded by, or
  is reviewed within a surrounding structure. That relation does not make the surrounding
  structure the primary subject. Center it only when the substantive body describes that
  structure's own rules, operation, or aggregate outcomes. If the context remains useful
  as a qualifier, the name must still state the focal work's reusable class.
- When the substantive body records a bounded undertaking with goals, execution, and
  outcomes, preserve its enduring work or activity class in the destination name. Do not
  reduce it to a thematic domain or name it after the reporting genre or occasion.
- Check a new name against its `SIGN`: every scope-bearing distinction needed to exclude
  a vetoed folder must occur in the name itself. Derive the `SIGN` only from the arriving
  documents and plausible siblings, never from traits of vetoed folders.
- Never choose the least-wrong existing folder, and never emit `SIGN` for an existing one.
Return tagged lines only.\
"""


@dataclass(frozen=True, slots=True)
class Folder:
    """A folder summary shown to the model."""

    path: PurePosixPath
    note: str
    documents: int
    held: int = 0
    children: int = 0


def _size(folder: Folder) -> str:
    """Describe direct and nested contents without making divided folders look empty."""
    if folder.children:
        return f"({folder.held} total, {folder.documents} direct, {folder.children} children)"
    return f"({folder.documents} documents)"


def _tree(folders: list[Folder]) -> str:
    if not folders:
        return "  (no folders; all documents are at the root)"
    lines = []
    for folder in sorted(folders, key=lambda item: str(item.path)):
        depth = len(folder.path.parts) - 1
        note = f" — {folder.note}" if folder.note else ""
        lines.append(f"  {'  ' * depth}{folder.path}/  {_size(folder)}{note}")
    return "\n".join(lines)


def _language_instruction(language: str) -> str:
    if not language:
        return ""
    return f"Write folder names and purposes in the documents' language: `{language}`.\n\n"


def build_review(*, folders: list[Folder], total: int, loose: int, language: str = "") -> Prompt:
    """Ask whether the tree needs structural maintenance."""
    return Prompt(
        system=_REVIEW,
        user=(
            f"{_language_instruction(language)}"
            f"DOCUMENTS: {total}\nLOOSE AT ROOT: {loose}\n\nTREE:\n{_tree(folders)}"
        ),
    )


def build_refiling(
    *,
    folder: PurePosixPath,
    children: list[Folder],
    documents: list[tuple[str, str]],
    remaining: int,
    language: str = "",
    must_place: bool = False,
) -> Prompt:
    """Ask how to divide the direct documents of one folder."""
    child_list = (
        "\n".join(
            f"  {(str(child.path) if must_place else child.path.name)}/  {_size(child)}"
            + (f" — {child.note}" if child.note else "")
            for child in sorted(children, key=lambda item: str(item.path))
        )
        or "  (no child folders)"
    )
    listed = "\n".join(f"  [{handle}] {line}" for handle, line in documents)
    current = f"{folder}/" if folder.parts else "ROOT"
    final_gate = f"\n\n{_ROOT_FINAL_GATE}" if must_place else ""
    return Prompt(
        system=_ROOT_REFILE if must_place else _REFILE,
        user=(
            f"{_language_instruction(language)}CURRENT FOLDER: {current}\n\n"
            f"EXISTING CHILDREN:\n{child_list}\n\n"
            f"DIRECT DOCUMENTS: {remaining}\n\n"
            f"DOCUMENTS TO REFILE ({len(documents)}):\n{listed}{final_gate}"
        ),
    )


RENAME = "current folder rename"


def parse_filing(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse document destinations and folder purposes from tagged lines."""
    placed: dict[str, str] = {}
    signs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        tag, separator, value = line.partition(":")
        tag, value = tag.strip(), value.strip()
        if not separator or not value:
            continue
        if tag.upper() == "SIGN":
            path, _, sentence = value.partition("|")
            if path.strip() and sentence.strip():
                signs[path.strip().strip("/")] = sentence.strip()
        elif tag.upper() == "RENAME":
            signs[RENAME] = value.strip().strip("/")
        elif tag.upper().startswith("D") and tag[1:].isdigit():
            placed[tag.upper()] = value
    return placed, signs


@dataclass(frozen=True, slots=True)
class Reviewed:
    """Structural changes requested by a tree review."""

    moves: tuple[tuple[str, str], ...] = ()
    refile: tuple[PurePosixPath, ...] = ()
    signs: Mapping[str, str] = field(default_factory=dict)

    @property
    def keep(self) -> bool:
        """Return whether the review requested no structural changes."""
        return not self.moves and not self.refile


def parse_review(text: str) -> Reviewed:
    """Parse structural actions from a review response."""
    moves: list[tuple[str, str]] = []
    refile: list[PurePosixPath] = []
    signs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        tag, separator, value = line.partition(":")
        tag, value = tag.strip().upper(), value.strip()
        if not separator or not value:
            continue
        if tag == "MOVE":
            source, _, target = value.partition("|")
            if source.strip() and target.strip():
                moves.append((source.strip().strip("/"), target.strip().strip("/")))
        elif tag == "REFILE":
            named = value.partition("|")[0].strip().strip("/")
            if named.upper() in {"ROOT", "(ROOT)", "."}:
                refile.append(PurePosixPath())
            elif named:
                refile.append(PurePosixPath(named))
        elif tag == "SIGN":
            path, _, sentence = value.partition("|")
            if path.strip() and sentence.strip():
                signs[path.strip().strip("/")] = sentence.strip()
    return Reviewed(tuple(moves), tuple(dict.fromkeys(refile)), signs)
