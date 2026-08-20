"""Two questions, and nothing else.

The whole job is a folder tree an agent can walk with ``ls``. That needs two decisions and
this module asks for exactly those:

* **where do these documents go**, asked of a handful at a time so the answer can see a
  class where a single document only shows a title;
* **is this tree still worth walking**, asked when the collection has grown enough that
  the answer could differ.

Both replies are plain tagged lines. A grammar compiled from a schema was measured to cost
the answer rather than shape it (docs/prior-art.md), and a line per document is the
smallest thing that can be parsed without one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from bismuth.ports.llm import Prompt

_FILING = """\
You are filing documents into a folder tree. The only reader is an agent with `ls`, \
`grep` and `read`: it lists a folder, sees the names of the folders inside it, and has to \
pick ONE without opening any of them. Every folder name is that reader's only evidence.

You are shown the tree as it stands and a handful of documents. Put each document \
somewhere.

This is what you are shown. Every folder that exists is one line:

  <path>/  (<how many documents sit in that folder itself>) — <what it says it holds>

That count is the folder's own documents and not what is under its sub-folders, so a \
folder with a large count standing beside its own sub-folders is one whose sub-folders are \
not holding much. A folder with no sentence after it has not been given one yet.

Every document to file is one line:

  [D<n>] <its own title> | <what kind of document it is> | <a few things it is about>

The handle is how you name it in your answer and means nothing outside this request. None \
of these lines is the document itself -- they are what was read out of it, so what you are \
comparing is subjects, not text.

Go through it in this order, for every document.

**1. Does a folder that already exists hold this?** If one does, that is the answer. A \
tree that grows a folder per document is the same list with a click in front of every \
entry, and a folder that exists is evidence that documents like this one have a home.

**2. If none does, look at the other documents in front of you.** They arrived together \
and you can see all of them at once, which is the only moment anything can. Two or more \
that belong together are a folder; name it and put them in it. This is where most new \
folders should come from.

**3. Only then, the root.** ROOT means: no folder here holds it, and nothing else in this \
batch belongs with it. It is a real answer and you should give it when it is true -- but it \
is the answer of last resort, not the safe one. A batch that answers ROOT for everything \
has decided nothing, and the pile it leaves is what the reader has to read instead of the \
tree.

**A folder name says what its documents are ABOUT.** Not what they are: their form, their \
kind, their date, who issued them, what rank of instrument they are. Those are true of \
almost everything and split nothing. A name that would be true of most of the collection \
excludes nothing.

**Nothing is named "other", "misc", "general" or "related".** The reader cannot tell what \
is inside, and everything that arrives later fits.

You may name a path that does not exist yet, and you may nest: `금융/은행법` puts a folder \
inside `금융`. Keep it shallow -- every level costs the reader a correct guess, and a wrong \
guess at any level never reaches what they wanted.

Before you answer, check the documents against each other once: any two that \
are about the same thing belong in the same place, whatever that place turns out to be.

Answer one line per document, and nothing else:

D1: <folder path, or ROOT>
D2: <folder path, or ROOT>

For every path you name that is not in the list above, add one line saying what it holds, \
written for someone who cannot see the documents:

SIGN: <folder path> | <one sentence>\
"""

_REVIEW = """\
You are looking at a folder tree that an agent walks with `ls`. Judge whether it is still \
worth walking, and say so.

Every folder is one line:

  <path>/  (<how many documents sit in that folder itself>) — <what it says it holds>

That count is the folder's own documents and not what is under its sub-folders, so a \
folder whose own count is larger than everything filed beneath it has not really divided \
anything.

The reader lists a folder, sees the names inside it, and picks one without opening any. \
So the tree is working when the names at each level divide what is under them, and failing \
when a reader cannot tell the names apart, when one folder holds most of the collection, \
when a folder's own pile is larger than everything filed under it, or when the same \
subject sits in two places.

Depth is not free: every level is another guess that has to be right, and a wrong one \
never recovers. Width is cheap by comparison -- a dozen clear names in one listing is one \
judgement, and a name that means nothing is worse than ten that do.

If the tree is good enough, say so and stop. **A tree that is merely imperfect is better \
than a tree redrawn every time someone asks**, because every redraw moves documents a \
reader may already have learned where to find.

If it is not, say what to move. You may move a folder under another, rename one by moving \
it to a new path, or lift one to the root. Documents travel with the folder they are in.

Answer either exactly:

KEEP

or a list of moves and nothing else:

MOVE: <path that exists now> | <where it should be>
SIGN: <new path> | <one sentence saying what it holds>\
"""


@dataclass(frozen=True, slots=True)
class Folder:
    """One line of the tree as the model is shown it."""

    path: PurePosixPath
    note: str
    documents: int
    """Sitting directly in it, which is what says whether its children are doing any work."""


def _tree(folders: list[Folder]) -> str:
    if not folders:
        return "  (the tree is empty; everything is at the root)"
    lines = []
    for folder in sorted(folders, key=lambda f: str(f.path)):
        depth = len(folder.path.parts) - 1
        held = f" — {folder.note}" if folder.note else ""
        lines.append(f"  {'  ' * depth}{folder.path}/  ({folder.documents} here){held}")
    return "\n".join(lines)


def build_filing(
    *,
    folders: list[Folder],
    documents: list[tuple[str, str]],
    loose: int,
    language: str = "",
) -> Prompt:
    """Where this handful of documents goes, in one call.

    A handful rather than one, because a class is only visible in several: asked about a
    single document the only honest answer is its title, and a tree of titles is the list
    the folders were supposed to replace.
    """
    listed = "\n".join(f"  [{handle}] {line}" for handle, line in documents)
    say = (
        f"These documents are written in `{language}`. Name folders and write signs in "
        f"`{language}`, using the words the documents use.\n\n"
        if language
        else ""
    )
    return Prompt(
        system=_FILING,
        user=(
            f"{say}FOLDERS THAT EXIST:\n{_tree(folders)}\n\n"
            f"DOCUMENTS SITTING AT THE ROOT, FILED NOWHERE: {loose}\n\n"
            f"DOCUMENTS TO FILE ({len(documents)}):\n{listed}"
        ),
    )


def build_review(*, folders: list[Folder], total: int, loose: int, language: str = "") -> Prompt:
    """Whether the tree is worth walking, asked when it has grown enough to answer differently."""
    say = (
        f"Write any folder name or sign in `{language}`, using the words the documents use.\n\n"
        if language
        else ""
    )
    return Prompt(
        system=_REVIEW,
        user=(
            f"{say}THE COLLECTION HOLDS {total} DOCUMENTS.\n"
            f"{loose} OF THEM ARE AT THE ROOT, FILED NOWHERE.\n\n"
            f"THE TREE:\n{_tree(folders)}"
        ),
    )


def parse_filing(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """``{handle: path}`` and ``{path: sign}`` from the reply.

    Unrecognised lines are dropped rather than guessed at. A handle nobody asked about, or
    a document with no line, is the caller's problem to notice -- this only reads.
    """
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
        elif tag.upper().startswith("D") and tag[1:].isdigit():
            placed[tag.upper()] = value
    return placed, signs


def parse_review(text: str) -> tuple[bool, list[tuple[str, str]], dict[str, str]]:
    """``(keep, [(from, to)], {path: sign})``.

    ``keep`` is the answer, not the absence of one: a reply with no moves in it leaves the
    tree alone, which is the same thing said two ways.
    """
    moves: list[tuple[str, str]] = []
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
        elif tag == "SIGN":
            path, _, sentence = value.partition("|")
            if path.strip() and sentence.strip():
                signs[path.strip().strip("/")] = sentence.strip()
    return not moves, moves, signs
