"""CREATE: drawing one class out of a pile, and routing a document behind one that stands.

Four steps, separated by what each may see. Grouping sees the documents without their
doc_type, because that is the property it must not group on. Naming sees the chosen group
and not the folder it sits in: a name that is never shown cannot be echoed. The axis is
asked about the class rather than the folder, and only the first time a folder divides.
"""

from __future__ import annotations

from bismuth.ports.llm import Prompt
from bismuth.prompts.subdivision.shared import (
    _SIGNS,
    _listing,
    _render_children,
    _render_documents,
    in_their_language,
)

_EMERGING_SYSTEM = f"""\
These documents have not been sorted and this folder has no sub-folders yet. You are \
deciding TWO things, and both outlive this answer.

**First the CLASS** -- one group that has gathered here and is worth a shelf. Describe \
it, then name it.

**Then the AXIS**, the one property that name is a value of. Every sub-folder this \
folder ever gets will be an answer to it and nothing later can change it, so you are \
fixing it now on a handful of files that may be an unrepresentative part of what this \
folder ends up holding. Return ONE property: not a comparison between candidates, not \
an explanation, and no domain rule the documents do not evidence.

So look for what these documents do NOT have in common. A property they all answer the \
same way divides nothing -- if the only thing two documents share is being the same kind \
of document, that is what they look like, not what this collection is organised by. A \
property they all answer DIFFERENTLY divides just as badly: a name, a title, a number, \
an identifier gives every folder one document and hands the reader the same list with a \
step in front of every entry. You want the property where a handful of answers cover \
everything here, several documents to each answer and none left without one.

If you cannot yet see which property will still matter when this folder is ten times \
larger, say so: `emerged` is false, nothing is created, and you will be asked again on \
the next arrival. Waiting costs one question. Choosing wrong costs the archive.

{_SIGNS}

**You are not dividing this folder and you are not accounting for every document.** \
Whatever does not belong to the class you name stays exactly where it is. Most of these \
documents staying put is the normal outcome, not a failure.

If two classes have grown, name the thicker; you will be asked again and the other can \
come out then. And if a broader class contains both, name that one instead -- the first \
shelf in a folder sets how wide the ones after it will be, and a narrow first shelf \
leaves the rest of the collection to be taken out in slivers. A shelf that turns out too \
broad gets split again later; a folder full of slivers is never repaired.

`emerged` is false when nothing has gathered yet -- common in a young archive, and the \
right answer more often than not. A collection whose documents are each about something \
different is a list, and a list is best left as a list. Say so.

**Answer `emerged` by reading the sign you have just written, not by asking whether you \
could name something.** You can always name something; that is not the question. The \
question is whether a class has gathered. So look at your own sign and answer false if:

- it lists several unrelated things, or reaches for a word like "various" to hold them \
together -- you have described a pile, not a class;
- it would fit every document in this folder -- then it is this folder, under a new name;
- it says what the documents are not, or what is left after something else was taken.

Saying false costs one more question, and you will be asked again on the next arrival. \
Saying true wrongly puts a folder in the archive that nothing later can remove.\
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

**Compare two numbers before you answer: how many signs are already here, and how many \
documents are still loose.** They pull in opposite directions and the answer depends on \
which is winning.

A big loose pile behind a handful of thin signs means this folder is still mostly an \
unsorted list. The reader gains nothing from three names when most of what they want is \
not behind any of them. **Here another sign is exactly what is needed, and declining \
leaves the pile where it is.**

**A sign that takes nearly everything is as useless as one that takes nearly nothing.** \
The reader gains exactly what they can rule out. If two of every three documents end up \
behind one name, they have ruled out a third of the collection and must still open the \
big folder -- and whatever is wrong with this folder is now wrong with that one, one \
level further in. Aim for shelves a reader would judge comparable in size: not the \
broadest name that fits, the broadest name that still leaves a real remainder behind.

**But a big pile calls for a broad sign, not one more narrow one.** Taking three \
documents out of fifty leaves a forty-seven-document pile and adds a name to read on the \
way past it -- the reader is worse off, not better. Look at the whole loose pile and ask \
what the largest part of it is a case of; name that, even though it is broader than the \
neat little group you can see. A broad shelf can be split again later, and will be. A \
thin one is permanent clutter.

Many signs with few documents behind each means the opposite: one long list has been \
replaced by a longer one, and another name makes the reader's first choice harder for \
no gain. Here the answer is no unless the new sign takes a real share of what is loose.

A handful of documents about the same one thing are not a class either way. A class is \
something you expect more of; if the honest name for the group is one document's subject, \
they stay where they are.

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


_GROUP_SYSTEM = """\
You are shown documents that sit together in one folder, described by their own cards. \
Find the ONE group among them that belongs together.

At least two documents, and never all of them. A group that takes everything is the \
folder itself under another name, and creates a level the reader clicks through to \
reach the same list. If no group has formed, return an empty list: nothing is created, \
and you will be asked again when the next document arrives. In a young collection that \
is the right answer more often than not.

Group by what the documents are ABOUT. A reader arrives wanting a subject. The shape of \
a document -- its format, the kind of instrument it is, who issued it, when -- is known \
for almost everything, so it fills a tree neatly and scatters each subject across every \
branch.

If several groups have formed, return the thickest one. The others come out later, on \
another arrival, and a group taken too narrow now leaves the rest to come out in slivers.

Return the handles exactly as shown, and one sentence saying what a further document \
would have to be about to belong with them. Say what they share, not what they are not, \
and not how you decided.\
"""

_CLASS_NAME_SYSTEM = """\
The documents you are shown all give the same answer to one question. Write that answer. \
It becomes the name of the folder they go into, and every folder beside it will be a \
different answer to the same question.

So write the answer, not the question back. A question about which industry a law \
regulates is answered by naming the industry, never by "the industry it regulates" -- \
that names the question, fits every document ever asked it, and sorts nothing.

Two or three words, in the words these documents use. A name that describes the act of \
arranging rather than what stands on the shelf -- the leftovers, the remainder, the ones \
grouped by subject -- leaves the reader with the same list and one more click to reach \
it. Nothing is named by what it is not: a shelf called "the ones that are not X" \
excludes nothing and can never be split further.

One folder, not a path. A name with a separator in it is thrown away.\
"""

_CLASS_SIGN_SYSTEM = """\
Write the one line printed under a folder name. The reader has already read that name, \
which is two or three words; this is what they read next to decide whether to open the \
folder or walk past it, so it has to say what those words could not.

Write it to someone standing outside the folder. They cannot see the documents and are \
not interested in how the folder was decided. So say what would belong here tomorrow, \
not what happens to be here today -- not what these particular documents have in common, \
not why this group and not another.

This line is also what the next document is judged against. When one arrives, it is \
shown the folder name and this line and asked whether it belongs. So it has to be usable \
for that: concrete about what falls under the folder. A line that says why the folder \
matters, or what its subject means for the future, answers nothing when a document is \
held up against it.

One sentence, two clauses at most, in the documents' own language. Do not open with a \
phrase about this folder or the documents in front of you.\
"""

_AXIS_BELOW_ROOT = """WHICH work a document belongs to is allowed HERE, and only here. This folder has already been narrowed to a subject, and a reader standing in it has chosen that subject; what they are looking for now is one work together with whatever supplements, amends or implements it, and those belong on one shelf. That question has several documents to each answer. It is forbidden at the top of a collection, where it gives every folder a single document -- but you are not at the top.

Never a yes-or-no. Asking whether a document is the one you just picked draws one shelf and leaves a remainder nobody can name except as the ones that are not that, and that remainder can never be divided again. If what the picked documents share is one work, the property is which work -- not whether it is that one."""

_AXIS_SYSTEM = """\
Some documents have been picked out of a folder because they share something. Write the \
question they all answer the same way -- the question that separates them from the rest \
of that folder.

Every folder that ever sits beside theirs will be another answer to this question, and \
nothing later can change it. So ask the one where a handful of answers cover a whole \
collection: several documents to each answer, and none left without one. Not a question \
every document answers the same way, which sorts nothing. Not one where every document \
has its own answer, which gives every folder a single document and hands the reader the \
same list with a step in front of every entry.

**The rest of the folder is listed below, and it has to be able to answer too.** Those \
documents are not part of the group you were shown; they are what the question will be \
asked about next, and every one of them will need an answer. A question that fits the \
picked documents perfectly and none of the others is the worst outcome available here: \
the folder is then divided by something almost nothing in it answers, and every later \
class is refused for answering a different question. Measured: a folder fixed on a property that fitted only the handful of documents which suggested it then refused every later class, correctly and uselessly, 103 times in one round.

A question a reader could ask of any document, ending in a question mark, in the \
documents' own language. Not an instruction, not a rule about folders, not a description \
of what you did. Then name the property it asks about, in a few words: a question about \
which industry a law regulates is asking about the industry regulated.\
"""


_GROUP_ALONG_SYSTEM = """\
You are shown documents that sit together in one folder, and the question that folder is \
already divided by. Find the ONE group among them that gives the same answer to it -- an \
answer that does not have a folder here yet.

At least two documents, and never all of them. If no answer has gathered enough \
documents, return an empty list: nothing is created, and you will be asked again when \
the next document arrives.

The question is not yours to change. A group that shares something else puts two kinds of \
distinction side by side, and then no folder name here rules anything out -- a reader has \
to open all of them, which is the cost the folders exist to avoid. If what has gathered is \
real but answers a different question, return an empty list; a later look at another level \
can take it.

Return the handles exactly as shown, and one sentence saying what a further document would \
have to be for this answer to fit it.

That sentence is a SENTENCE, never a title. 217 of 381 replies answered with the name of \
one law, and everything downstream is built from this line: the property the folder is \
divided on, the folder's name, and the sign a reader is shown. A title in this slot puts \
one work's name on a shelf that will be asked to hold others.\
"""


def build_group(
    *,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
    axis: str = "",
    axis_question: str = "",
    language: str = "",
) -> Prompt:
    """Step one: which of these belong together? No folder name, no name to write.

    With an axis the folder is already divided and the question narrows to "another
    answer to the same question?" -- which is a smaller question than "what do these
    share", and the only one a divided folder is allowed to ask.
    """
    # The bounds as numbers rather than as prose. "At least two, never all of them" was
    # ignored 84 times in 567 replies -- one document returned as a group 51 times, the
    # whole packet 33 times -- and a bound a model has to count out is a bound it does
    # not check.
    most = max(2, len(documents) - 1)
    user = (
        f"DOCUMENTS ({len(documents)}):\n{_render_documents(documents)}\n\n"
        f"RETURN 0 HANDLES, OR BETWEEN 2 AND {most} OF THEM. NEVER ALL {len(documents)}."
    )
    if children:
        user += "\n\n" + _render_children(children)
    if not axis:
        return Prompt(system=_GROUP_SYSTEM, user=in_their_language(user, language))
    # No question is invented when the folder has none recorded. It used to be built from a
    # Korean template, which would have put a Korean sentence in front of an English archive
    # -- the collection's language is read off its cards, never assumed (SPEC.md 2).
    asks = f"\nTHE QUESTION ITS FOLDERS ANSWER: {axis_question}" if axis_question else ""
    return Prompt(
        system=_GROUP_ALONG_SYSTEM,
        user=in_their_language(f"THIS FOLDER IS DIVIDED BY: {axis}{asks}\n\n{user}", language),
    )


def build_class_name(
    *,
    shared: str,
    question: str,
    documents: list[tuple[str, str]],
    taken: list[str],
    language: str = "",
) -> Prompt:
    """Step three: the group's answer to the question, which becomes the folder name.

    Neither the enclosing folder's name nor its own property phrase is in this prompt --
    the first was echoed 75 times in 81 replies, the second 72 times in 80. What is here
    is a question, and the reply is an answer to it.
    """
    user = (
        f"THE QUESTION THESE DOCUMENTS ANSWER: {question}\n\n"
        f"WHAT THEY SHARE: {shared}\n\nTHEM:\n{_render_documents(documents)}"
    )
    if taken:
        user += "\n\nANSWERS ALREADY TAKEN BY FOLDERS BESIDE THIS ONE:\n  " + "\n  ".join(taken)
    return Prompt(system=_CLASS_NAME_SYSTEM, user=in_their_language(user, language))


def build_class_sign(
    *, shared: str, documents: list[tuple[str, str]], language: str = ""
) -> Prompt:
    """Step four: the line under the name, written from the documents but not the name.

    Shown the name, the sign came back as the name again 22 times in 349, empty 9 more,
    and once past the note budget -- the same failure as every other step handed the
    string it must not produce. So the name stays out.

    The documents did not. Given only the group's sentence, this step invented: from
    `shared` = "중소벤처기업부" it wrote "중소기업 및 벤처기업 지원 정책과 사업 공고가
    담긴 문서", and the documents were the full text of 시행규칙. Membership judges each
    document against the sign, so every one of them correctly answered STAY -- including
    the three the group was built from. One folder spent 5,125 questions that way and
    shelved nothing. A sign is a promise about documents and cannot be written without
    seeing them.
    """
    listed = _render_documents(documents)
    return Prompt(
        system=_CLASS_SIGN_SYSTEM,
        user=in_their_language(
            f"WHAT THE DOCUMENTS IN THIS FOLDER SHARE: {shared}\n\nTHEM:\n{listed}",
            language,
        ),
    )


def build_axis(*, shared: str, rest: list[str], language: str = "", is_root: bool = True) -> Prompt:
    """Step two, and only when a folder divides for the first time.

    Before the name exists, so there is no name to hand back. Asked after the name, it
    came back as the name itself in 72 of 80 replies and every division was refused.

    The documents are still not shown. With them, one root chose 시행규칙이 규정한 거래
    유형 -- an axis naming the kind of instrument, read straight off the titles -- and
    every folder underneath became an answer to it. ``rest`` is not the documents: it is
    the subject vocabulary of everything in the folder the group did not take, which is
    what the question will have to be asked about next. Asked from the group alone, this
    step chose a question the group answered and the other 290 documents could not, and
    the folder could never divide again.
    """
    listed = ", ".join(rest)
    return Prompt(
        system=_AXIS_SYSTEM if is_root else _AXIS_SYSTEM + "\n" + _AXIS_BELOW_ROOT,
        user=in_their_language(
            f"WHAT THE PICKED DOCUMENTS SHARE: {shared}"
            + (f"\n\nWHAT THE REST OF THE FOLDER IS ABOUT: {listed}" if listed else ""),
            language,
        ),
    )


def build_emerging(
    *,
    path: str,
    purpose: str,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
    axis: str = "",
    spent: list[str] | None = None,
    language: str = "",
) -> Prompt:
    """Step one: has any one class grown thick enough to come out?

    With an ``axis``, the folder has been divided before and the question narrows to
    "another answer to the same question?". Without one, the axis is chosen here and
    every sub-folder this folder ever gets is held to it.

    A folder that keeps proposing refused names is not asked differently here. Showing it
    what it had already been refused was measured and made things worse: the refusals
    changed kind rather than stopping -- from an ancestor's name to a failed semantic
    audit -- while the longer prompt quadrupled the run's model calls, 44,399 for 300
    documents against 9,928. A folder with no good answer left does not have one shown
    back to it.
    """
    user = _listing(path, purpose, documents, children)
    if not axis:
        if spent:
            user += (
                "\n\nPROPERTIES ALREADY USED ABOVE THIS FOLDER (do not reuse them here):\n  "
                + "\n  ".join(spent)
            )
        return Prompt(system=_EMERGING_SYSTEM, user=in_their_language(user, language))
    return Prompt(
        system=_EMERGING_ALONG_SYSTEM,
        user=in_their_language(f"{user}\n\nTHE AXIS HERE: {axis}", language),
    )


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


"""One check per call. Three booleans in one reply came back all-false on boundaries whose
signs were specific and correct; asked one at a time, against the same evidence, each has
a single thing to weigh. This is the SPEC 2.1 contract applied to the one question that
can destroy structure."""


def build_existing_choice(
    *,
    path: str,
    document: tuple[str, str],
    axis: str,
    axis_question: str,
    children: list[tuple[str, str]],
) -> Prompt:
    """Route one loose document with a closed, non-JSON decision.

    Each sign carries the note that is on that folder. The name alone is two or three
    words and suggests more than it means: shown only names, this question sent
    가상자산 이용자 보호 등에 관한 법률 시행령 into 데이터 산업 관련 법령 -- crypto reads
    as digital from the name, and the note that would have ruled it out ("데이터 산업의
    진흥, 거래, 품질 관리 및 관련 위원회 운영") was never in the prompt. The folder then
    twice tried to draw 가상자산 out of itself and was refused both times, because one
    document is not a class. Nothing takes a wrongly filed document back out.
    """
    signs = "\n".join(
        f"  [F{index:03d}] {name}/" + (f" — {note}" if note else "")
        for index, (name, note) in enumerate(children, start=1)
    )
    return Prompt(
        system=(
            "Route this one loose document only when an existing sign positively describes it. "
            "Reply with exactly one shown F### handle or STAY. STAY is normal when no sign is a "
            "clear fit. Do not explain, rename, or create a sign.\n\n"
            "Judge against the note under each name, not the name alone: a name is two or "
            "three words and will suggest more than it means. A document filed behind a sign "
            "that does not describe it is worse than one left loose, because the sign then "
            "lies to every reader who trusts it."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"SIGNS:\n{signs}\n\nDOCUMENT:\n  [{document[0]}] {document[1]}"
        ),
    )


def build_member_choice(
    *, path: str, purpose: str, document: tuple[str, str], name: str, sign: str = ""
) -> Prompt:
    """Decide membership in one new class without echoing a document ID.

    The sign goes in, not just the name. Deciding from two or three words is deciding
    from whatever those words suggest: 가상융합산업 진흥법 시행규칙 was shelved under
    이공계인력지원 because it mentions training specialists, and the folder that resulted
    could never be divided again -- no honest property separates workforce-support law
    from virtual-convergence-industry law, because they were never one class. The sign is
    the scope contract the rest of this module exists to produce, and membership was the
    one question that never saw it.
    """
    return Prompt(
        system=(
            "A new library class has been named, and its sign says what belongs behind it. "
            "Decide whether this one document genuinely belongs there. Reply with exactly "
            "SHELF or STAY. Do not explain.\n\n"
            "Judge against the sign, not the name alone: a name is two or three words and "
            "will suggest more than it means. SHELF only when the sign positively describes "
            "this document. STAY when it does not -- the document stays safely where it is, "
            "which is the normal answer for most documents, and a wrong SHELF is worse than "
            "a STAY because the shelf then holds something its own name denies."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nPURPOSE: {purpose or '(none)'}\n"
            f"NEW SIGN: {name}/\n"
            f"WHAT BELONGS BEHIND IT: {sign or '(no sign was written)'}\n\n"
            f"DOCUMENT:\n  [{document[0]}] {document[1]}"
        ),
    )


"""One check per call, each ending on the condition that HOLDS.

Three phrasings were measured. Six booleans in one reply approved the axes 문서의 성격,
주관 부처 and 법령의 성격 -- the three this exists to reject. Split into closed calls but
phrased as questions, three checks failed nearly everything. Split and phrased as a
statement followed by "FAILS if ...", five of six failed nearly everything and the archive
stayed flat at thirty documents: the answer was tracking the last condition read, which is
the same recency this repository already handles by generating a verdict field last."""


def build_existing_assignments(
    *,
    path: str,
    documents: list[tuple[str, str]],
    axis: str,
    axis_question: str,
    children: list[tuple[str, str]],
) -> Prompt:
    """Ask whether loose documents already belong behind an existing direct sign."""
    handled_children = [
        (f"F{index:03d}", name, note) for index, (name, note) in enumerate(children, start=1)
    ]
    rendered_children = "\n".join(
        f"  [{folder_id}] {name}/" + (f" — {note}" if note else "")
        for folder_id, name, note in handled_children
    )
    return Prompt(
        system=(
            "This folder already has sub-folders along one recorded axis. Route only loose "
            "documents that clearly belong under an existing direct sub-folder. Return only "
            "the shown F### handle; never copy or compose a folder name. Do not create or "
            "rename a class, change the "
            "axis, or account for every document. An empty result is normal. A document "
            "that merely fits one sign better than the others stays loose."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"EXISTING DIRECT SUB-FOLDERS:\n{rendered_children or '  (none)'}\n\n"
            f"LOOSE DOCUMENTS:\n{_render_documents(documents)}"
        ),
    )
