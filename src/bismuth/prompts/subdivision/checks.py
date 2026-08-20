"""The two closed questions a proposal is held to before anything moves.

Each is asked once, at the moment the thing it judges is decided, and never again. Asked
as one of six booleans in a single reply they approved every axis they exist to reject;
split into their own calls and phrased as a statement followed by "FAILS if ...", they
work.
"""

from __future__ import annotations

from bismuth.ports.llm import Prompt

_NAME_CHECK_SYSTEM = """\
A folder is about to be created, and you are checking ONE thing: whether its name is an \
ANSWER to the question this folder's siblings all answer. Answer with exactly ANSWERS or \
BESIDE and nothing else.

The question is fixed. It was chosen when this folder was first divided and every folder \
standing here is one answer to it, which is what lets a reader rule the others out by \
reading the names alone. A name that answers some different question sits BESIDE that \
list rather than in it: the reader can no longer tell what the names are distinguishing, \
and has to open everything again.

BESIDE if the name is the title of one law, work, series or programme, when the question \
asks about something else -- who it applies to, what it regulates, what field it is in. \
A title is a name, not an answer. Where the question asks who a document applies to, a folder named after one work answers something else, and a reader scanning for their own situation learns nothing from it.

BESIDE if the name answers a different question about the same documents: their form, \
their issuer, their date, or which body administers them, when that is not what was \
asked.

ANSWERS if a reader could put the name and the question together into a sentence that is \
true of the documents behind it, and false of the folders standing beside it.\
"""


def build_name_check(*, path: str, question: str, name: str, taken: list[str]) -> Prompt:
    """One closed question about a name, before the documents are asked to join it.

    The axis is checked when it is chosen and never again, so everything after it was
    free: the question could be about who a law applies to and the answers could be the
    titles of laws, for ever. This was one of the six booleans in the audit that went out
    with the redraw path -- the one that reads only the names, which is why it survives
    on its own where the others needed a whole boundary in front of them.
    """
    return Prompt(
        system=_NAME_CHECK_SYSTEM,
        user=(
            f"FOLDER: {path or '(root)'}\n"
            f"THE QUESTION ITS FOLDERS ANSWER: {question}\n"
            f"THE PROPOSED NAME: {name}\n"
            + (
                "ANSWERS ALREADY TAKEN BY FOLDERS STANDING HERE:\n"
                + "\n".join(f"  {item}" for item in taken)
                if taken
                else "NO FOLDER STANDS HERE YET."
            )
        ),
    )


_AXIS_CHECK_SYSTEM = """\
A library folder is about to be divided, and you are checking ONE thing: whether the \
property it is being divided on is a good one to divide on HERE. Answer with exactly \
FAILS or HOLDS and nothing else.

Some properties are known for nearly every document -- what kind of document it is, what \
form it takes, who issued it, when it was issued, what language it is in, and the title \
of the one law or work it belongs to. Sorting by one of those produces a tidy tree and a \
useless one: a reader who arrives wanting a subject finds that every subject has been \
spread evenly across every folder, so they must open all of them. The tree looks \
organised and narrows nothing.

Sorting by what the documents are ABOUT does the opposite. A reader who wants one \
subject opens one folder.

FAILS, anywhere and at any depth, if the property is what the documents ARE rather than \
what they are about: the kind of document, its form, its rank in a hierarchy of \
instruments, who issued it, when, or in what language. These are known for almost every \
document, so they fill a tree neatly and scatter each subject across all of it. A folder that is already about a subject is not licensed to sort by form inside itself: its children then name kinds of document, which tell a reader looking for a subject nothing at all.

WHICH single law or work a document belongs to is the one exception, and only below the \
root. At the root it gives every folder one document and hands the reader the same list \
with a step in front of every entry. Inside a folder already about a subject, a shelf \
holding one law's act, its decree and its rules is something a reader who has already \
chosen that subject can use.

FAILS if what is offered is not the NAME of a property at all: a sentence describing the \
split, a comparison between two candidates, or an explanation of why it was chosen. A \
property is named in a few words, the way a column heading is.

FAILS if the property is one the folders ABOVE are already divided on. Those are listed. \
Every document here already has the same answer to them, so dividing on one again \
separates nothing and only restates the parent's name in other words.

Sharing a WORD with an ancestor's property is not the same thing. A property that narrows an ancestor's to one aspect of it asks a different question, and it HOLDS. The test is \
whether the documents in front of you would give different answers to it -- not whether \
it reads like something above.

FAILS if almost every document here would give the SAME answer to it. That draws one \
real shelf and a remainder nobody can name except as "the ones that are not that" -- a \
folder holding everything except one thing, which excludes nothing and cannot be divided \
again. Inside a folder already narrowed to one subject, dividing on who issued the documents produces one shelf and, beside it, a folder that can only be named for not being that.

HOLDS if the property is about what the documents are about. HOLDS also when this \
folder has already been narrowed by subject and the property is a sensible way to split \
what remains -- but only if the documents here really do spread across several of its \
answers. Standing inside a subject licenses a different question, not one whose answer \
is already fixed for nearly everything in the folder.\
"""


def build_axis_check(
    *,
    path: str,
    axis: str,
    axis_question: str,
    name: str,
    rest: list[str] | None = None,
    spent: list[str] | None = None,
) -> Prompt:
    """One closed question about the property a folder is about to be fixed on.

    Asked once, when the axis is chosen, and never again: after that the folder is
    divided and every later class answers a question that has already been checked.

    Separated from everything else deliberately. Asked as one of six booleans in a single
    reply, it approved 문서의 성격, 주관 부처 and 법령의 성격 -- every axis it exists to
    reject. It went out with the boundary audit and the next run fixed the root on 법률명
    at 89 documents, which is the failure docs/spec/subdivision.md 9-2 records as fatal.

    ``rest`` is what the folder is about, which the system prompt has always assumed it
    could see: two of its rules are about whether the documents here would give different
    answers, and it was shown a path and a label. Reading the label alone it held
    상생협력 촉진 분야 over a folder of 과학관법, 디지털포용 and 가상융합산업 -- a
    well-formed subject property, and one nothing in that folder answers -- which then
    fixed six folders on it and left 55 documents unable to divide.
    """
    return Prompt(
        system=_AXIS_CHECK_SYSTEM,
        user=(
            f"FOLDER: {path or '(root)'}\n"
            + ("WHAT THE DOCUMENTS HERE ARE ABOUT:\n  " + ", ".join(rest) + "\n" if rest else "")
            + "PROPERTIES THE FOLDERS ABOVE ARE ALREADY DIVIDED ON:\n"
            + ("\n".join(f"  {item}" for item in spent) if spent else "  (none)")
            + f"\n\nPROPERTY: {axis}\nQUESTION IT ASKS: {axis_question}\n"
            f"FIRST FOLDER NAME IT WOULD PRODUCE: {name}"
        ),
    )
