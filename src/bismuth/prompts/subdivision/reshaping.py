"""MERGE and SPLIT: the two operators that move folders instead of documents.

No document changes the folder it is in; the path above it gets one level longer or one
shorter. That is why these can be asked where redrawing a boundary cannot -- there is
nothing here for a wrong answer to scramble, only a level to add or not add.
"""

from __future__ import annotations

from bismuth.ports.llm import Prompt
from bismuth.prompts.subdivision.shared import _SIGNS, in_their_language

_COVERS_CHECK_SYSTEM = """\
A folder that already stands is about to have another folder moved inside it. You are \
checking ONE thing: whether the standing folder's name still answers for what would be \
under it once the move is made. Answer with exactly COVERS or WIDER and nothing else.

The name on a folder is the only thing a reader has before opening it. Moving something \
in that the name does not reach does not make the name broader -- it makes it wrong, and \
every reader who trusts it walks past what they came for.

WIDER if the standing name is about a different property of the documents than the \
incoming folder is: who issues them against what they are about, their form against their \
subject, the organisation that keeps them against the field they belong to. A folder named for an issuing body says nothing about the subjects its documents cover, so a subject folder moved under it stops being findable.

WIDER if a reader looking for what is in the incoming folder would not think to open the \
standing one.

COVERS if the incoming folder is one more instance of what the standing name already \
says, so that a reader who wants it would open that folder first.\
"""


def build_covers_check(*, shelf: str, note: str, incoming: str, incoming_note: str) -> Prompt:
    """Whether a folder that already stands answers for what is about to move inside it.

    The other half of merge has no name to invent, so nothing checked it: a new shelf is
    named from its members and cannot fail to cover them, while a standing one was named
    before they existed. That asymmetry moved a 42-document research subtree under an
    organisation chart in a 300-document run.
    """
    return Prompt(
        system=_COVERS_CHECK_SYSTEM,
        user=(
            f"THE FOLDER THAT STANDS HERE: {shelf}\n"
            + (f"WHAT IT SAYS IT HOLDS: {note}\n" if note else "")
            + f"THE FOLDER THAT WOULD MOVE INSIDE IT: {incoming}\n"
            + (f"WHAT THAT ONE SAYS IT HOLDS: {incoming_note}\n" if incoming_note else "")
        ),
    )


_GROUPING_SYSTEM = f"""\
This folder has grown a long list of sub-folders. A reader must read that whole list \
before they see a single document, so the list itself is now the thing costing them \
time. You are deciding ONE thing: whether several of the folders that already exist \
belong together under one broader name.

Nothing is being re-sorted. No document changes the folder it is in. The folders you \
name keep their own names and their own contents and simply stand together on one shelf \
instead of separately on this one.

**Look for the folders that answer the same part of the question.** Several narrow names \
that a reader would only ever reach by the same route are the case for this: the broader \
name goes on the outside, and the reader who does not want it skips all of them at once, \
which is exactly what they could not do before.

**A group of two barely earns it.** Replacing two names with one that then contains them \
leaves the reader the same number of choices; it just moves one of them. Look for enough \
of them that the list actually gets shorter.

**Not everything.** Some folders must stay where they are, or you have renamed this \
folder rather than tidied it, and the reader gains a level that rules nothing out.

**The shelf may be a folder that already stands here.** If one of these folders is \
already the broader class the others belong under, name it: they move inside it and no \
new level is created at all. That is the cheapest answer available to you -- the reader \
loses a name from the list and gains nothing to click through. A folder cannot stand \
inside itself, so do not also list it among the ones that move.

**The broader name must be a real class, not a container word.** It is the answer to the \
same question the folders under it answer, one step up. If the only name that covers them \
is a word meaning "assorted", they do not belong together.

{_SIGNS}

`emerged` is false when these folders are already the right list -- when no group of them \
shares anything a reader would recognise from outside. That is a normal answer and a safe \
one: a list that is merely long is better than a level that is merely wrong.\
"""


_SPLIT_SYSTEM = """\
EVERY VALUE YOU RETURN IS WRITTEN IN THE DOCUMENTS' OWN LANGUAGE.

A reader walking to a document must guess right at every level on the way. You are looking \
at one level and deciding whether it earns that guess. Answer with exactly DISSOLVE or \
KEEP and nothing else. Do not explain.

DISSOLVE means this folder goes away and everything standing in it moves up one step, \
beside what already stands there. Nothing is thrown away and no document leaves the folder \
it is in -- the path above it gets shorter by one.

Answer DISSOLVE when the level adds a guess and rules nothing out:

- its name says again what the folder above it already said, in other words;
- a reader who wanted anything inside it would have had to open it anyway, because its \
name does not separate it from what stands beside it;
- it holds one thing, so choosing it is not a choice;
- its name describes the sorting rather than what stands on the shelf.

Answer KEEP when a reader can look at its name beside its siblings' and rule it out \
without opening it. A level that does that is worth the guess, however few documents are \
behind it.\
"""


def build_split_check(
    *,
    path: str,
    note: str,
    children: list[tuple[str, str, int]],
    documents: int,
    parent: str,
    parent_note: str,
    siblings: list[tuple[str, str]],
    language: str = "",
) -> Prompt:
    """One closed question: does this level earn a reader's guess?

    The reverse of :func:`build_grouping`. Shown what would land beside what, because that
    is the whole question -- a name can only rule something out next to the names it would
    stand with.
    """
    inside = (
        "\n".join(
            f"  {name}/  ({count})" + (f" — {note}" if note else "")
            for name, note, count in children
        )
        or "  (none)"
    )
    beside = (
        "\n".join(f"  {name}/" + (f" — {note}" if note else "") for name, note in siblings)
        or "  (none)"
    )
    return Prompt(
        system=_SPLIT_SYSTEM,
        user=in_their_language(
            f"THE LEVEL IN QUESTION: {path}\n"
            + (f"ITS SIGN: {note}\n" if note else "")
            + f"IT HOLDS {documents} document(s) of its own, and these folders:\n{inside}\n\n"
            f"IF DISSOLVED, ALL OF THAT MOVES UP INTO: {parent or '(root)'}\n"
            + (f"WHOSE SIGN IS: {parent_note}\n" if parent_note else "")
            + f"AND WOULD STAND BESIDE:\n{beside}",
            language,
        ),
    )


def build_grouping(
    *, path: str, children: list[tuple[str, str, int]], axis: str, language: str = ""
) -> Prompt:
    """Ask whether existing sub-folders should stand together under one broader name."""
    rendered = "\n".join(
        f"  {name}/ — {count} documents" + (f" — {note}" if note else "")
        for name, note, count in children
    )
    return Prompt(
        system=_GROUPING_SYSTEM,
        user=in_their_language(
            f"FOLDER: {path or '(root)'}\n"
            f"THE QUESTION THESE SUB-FOLDERS ANSWER: {axis or '(none recorded)'}\n"
            f"SUB-FOLDERS STANDING HERE ({len(children)} of them, all read before any "
            f"document):\n{rendered}",
            language,
        ),
    )


_GROUPING_MEMBER_SYSTEM = """\
A broader shelf has been decided on and named. You are looking at ONE folder that stands \
beside it now, and saying whether it moves onto that shelf. Answer with exactly SHELF or \
STAY and nothing else.

SHELF when a reader who read the broader sign would expect to find this folder behind it.

STAY when they would not. Staying is the normal answer for most folders and costs \
nothing: the folder keeps its place exactly as it is.\
"""


_SHELF_CHECK_SYSTEM = """\
Folders are about to be stood together under one broader name. You are checking ONE \
thing: whether that name is a CLASS a reader could arrive wanting, or only a word for \
what these documents ARE. Answer with exactly CLASS or CONTAINER and nothing else.

CONTAINER if the name is about the form of the documents rather than their subject -- \
the kind of instrument, its rank, whether it is an act, a decree or a rule. A name that is true of almost every document in the collection excludes nothing, and a shelf built on one swallows the archive.

CONTAINER if the name leans on a word meaning assorted, individual, various, other, \
related or general, with or without a subject attached to it. The reader cannot tell \
what is inside from such a name, and the next arrival always fits it.

CONTAINER if nearly every folder standing here would fit the name, including the ones \
that are staying behind. A shelf everything fits is this folder under another name, and \
the reader clicks through it to reach the same list.

CLASS if the name is a subject, an activity, a party or an industry -- something a \
reader arrives already wanting, and something the folders staying behind are not.\
"""


def build_shelf_check(
    *, path: str, name: str, sign: str, moving: list[str], staying: list[str]
) -> Prompt:
    """One closed question about the broader name, before anyone is asked to move.

    Grouping is the one operator that invents a name without choosing a property, so
    none of the rules the axis check holds a division to reach it. Left unchecked it
    built the collection's top level out of what the documents are made of.

    Asked before the membership loop, which costs one call per folder standing here.
    """
    return Prompt(
        system=_SHELF_CHECK_SYSTEM,
        user=(
            f"FOLDER: {path or '(root)'}\n"
            f"THE BROADER NAME: {name}\n"
            + (f"WHAT IT WOULD SAY: {sign}\n" if sign else "")
            + "FOLDERS STANDING HERE:\n"
            + "\n".join(f"  {item}/" for item in moving + staying)
        ),
    )


def build_grouping_member(
    *, path: str, name: str, sign: str, child: tuple[str, str, int]
) -> Prompt:
    """One closed question about one existing sub-folder."""
    child_name, note, count = child
    return Prompt(
        system=_GROUPING_MEMBER_SYSTEM,
        user=(
            f"FOLDER: {path or '(root)'}\n"
            f"THE BROADER SHELF: {name} — {sign}\n\n"
            f"THE FOLDER IN QUESTION: {child_name}/ — {count} documents"
            + (f" — {note}" if note else "")
        ),
    )
