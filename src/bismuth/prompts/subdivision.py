"""Asking what has grown here, and who belongs to it.

The shape of each question is the point, and the shape is not "divide this".

**Normal growth is not a partition.** A partition has to account for every document, and
a heterogeneous pile cannot honestly be accounted for without inventing a remainder
class. Forbidding remainder names does not work, because the partition demands one.

So instead one class is drawn out at a time (docs/spec/subdivision.md 2, "새 범주 추가").
*Emerging* names a single class and nothing else; *Members* says who belongs to that one
class. Neither reply has a slot the leftovers could go in, and the leftovers stay in the
folder they are already in, which is what SPEC.md 3.4 says should happen to them.

Asking this repeatedly is safe in a way that asking "how would you divide this" is not:
it can add one sibling or route a loose document behind an existing sign, but it cannot
change the axis or redraw existing siblings.

No question here redraws a boundary any more. Redrawing one is the only operation that
moves a document that is already filed, it needs the whole subtree in the prompt to be
asked at all, and its verdict was measured to follow the length of that prompt rather
than its contents. It belongs to the whole-collection pass, which can see what a folder
cannot (docs/spec/maintenance.md 4). What is left here is additive: draw one class out,
route a loose document behind a sign that already stands, stand folders together, or
dissolve a level.

There is no free-form reasoning metadata in these schemas. Emerging writes its concrete
candidate before its verdict so constrained decoding does not commit before identifying
what allegedly emerged.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, Field, field_validator

from bismuth.domain.maintenance import is_axis_label
from bismuth.ports.llm import Prompt

"""What a folder name has to do, shared by every prompt that asks for one.

Each rule here was added after a measured failure, and the evidence for it lives in the
git history rather than in the tokens: the compound-name rule after a folder called
연구인프라 및 인력 지원 was given the child 국가연구인프라 및 인력 지원 with the rule already
in the prompt; the sign-is-a-sentence rule after fifty notes in one 300-document round
reached disk as the folder's own name written twice; the no-separator rule after names
arrived as paths and landed a level up from where they were meant.

Sent on every subdivision call and embedded in five system prompts, so a paragraph here
costs five times its length. Rationale belongs in this docstring; the string below holds
instructions only.
"""

_SIGNS = """\
Think of folders as SIGNS, not as groups.

A reader who arrives sees the document list. Divided, they see a few folder names \
INSTEAD, and must pick one before they see any document. The division is worth making \
only if those names let them ignore most of the collection.

So a division fails when there are nearly as many signs as documents, when a sign \
points at one document, or when most documents fit no sign. A sign names a CLASS -- \
something you expect more of -- never one document's subject.

**One sign says one thing.** Several unrelated things strung together is an inventory \
of what happened to land here: the reader cannot tell which part is meant, so they open \
it anyway, and the next arrival either extends the list or forces a rewrite. If you can \
only name the group by listing it, name instead the one thing they are all a case of, \
or take the thickest alone and leave the rest loose.

**Name a KIND of this folder, never this folder again.** Every document here already \
answers to the folder's own name, so that name -- in other words, or with a qualifier \
added in front or behind -- rules nothing out, and a class covering every document \
moves the whole pile one level down to face the same list again. A kind is named by \
what makes it that kind: different words, not more of the same ones. Some documents \
must stay behind for a division to have happened. If nothing narrower than this folder \
fits, nothing has emerged; say so.

**Nothing is named by what it is not.** "The ones that are not X" excludes nothing and \
can never be split further, and a reader knows what they want, not what they do not. \
Two habits produce it: splitting on a property nearly every document answers the same \
way, which leaves one shelf and a remainder, and describing a class only against the \
one beside it, which makes that other class the distinction. **Name the shelf, not the \
sorting** -- the leftovers, the remainder, the ones sorted by subject are not groups. \
If the honest name is "the rest", say nothing emerged.

**Ask what the documents are ABOUT first.** Format, type, language, date and issuing \
body are known for almost everything, so they fill a tree neatly and leave the reader \
no better off; drawn first they scatter each subject across every branch. They are \
usable LATER, inside a shelf that is already about something.

**Every level costs the reader a correct guess.** A shelf here sits behind every choice \
above it, and a wrong guess at any one of them never reaches it. Deep down the bar is \
higher, not lower. **The name is one folder, not a path** -- a separator in it throws \
the whole answer away.

**The sign is a sentence; the name is a label.** You write the sign first, so there is \
no name yet to repeat: if what you just wrote could itself serve as a folder name, it \
is the name arriving early, not a sign. Write it to someone standing outside the folder \
who cannot see these documents and does not care how you decided -- what would belong \
here tomorrow, not what happens to be here today. Not what you noticed about the pile, \
not why this class and not another. One sentence, two clauses at most; past that it is \
thrown away and the folder is labelled with its own name twice. If you cannot say it in \
one sentence, the class is not clear enough yet.

Signs and notes in the DOCUMENTS' OWN LANGUAGE. If the documents are in one language, \
the sign is in that language too.\
"""

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

_LISTING = """\
FOLDER: {path}
{already}{purpose}
DOCUMENTS SITTING DIRECTLY HERE ({count}):
{documents}
{children}\
"""


class Gathered(BaseModel):
    """The documents in a folder that belong together, before anything is named.

    Naming is not asked here. The old single call asked for the group, its name, the
    folder's permanent axis, that axis as a question, and a verdict, all at once -- and
    to name the group it had to be shown the enclosing folder's name, which is the one
    string it must not return. gpt-5-nano returned it in 75 of 81 replies.

    Members before the sentence: the sentence is about a group, and written first it is
    written about nothing.
    """

    members: list[str] = Field(
        default_factory=list,
        description=(
            "The handles of the documents that belong together, exactly as shown. "
            "Empty when no group has formed."
        ),
    )
    shared: str = Field(
        default="",
        description=(
            "One sentence: what a further document would have to be about to belong "
            "with these. In the documents' own language."
        ),
    )


class ClassName(BaseModel):
    """A folder name for a group that has already been chosen.

    The enclosing folder's name is deliberately absent from this prompt. It used to be
    shown twice -- once as the address, once as a capitalised warning not to repeat it --
    and three paragraphs of the system prompt argued against repeating it. A name that is
    never shown cannot be returned. Narrowness is carried by the input instead: the group
    is a strict subset, so a name that fits it fits fewer documents than the folder's own.
    """

    name: str = Field(default="", description="Two or three words, in the documents' own language.")


class ClassSign(BaseModel):
    """The line printed under a folder name, written once the name exists.

    Separate from the name because it is written for a different reader: the name is an
    address, the sign is what someone outside the folder reads to decide whether to open
    it. Written in the same call, it arrived as the name again fifty times in one round.
    """

    sign: str = Field(default="", description="One sentence, in the documents' own language.")


class Axis(BaseModel):
    """The question a folder's children all answer, fixed the first time it divides.

    Asked about the class that just emerged rather than about the folder, so the property
    the enclosing folder was already divided on is not the salient answer. That means the
    spent properties no longer have to be listed in order to be forbidden.
    """

    axis_question: str = Field(default="", description="Ends in a question mark.")
    axis: str = Field(
        default="", description="The property that question asks about, in a few words."
    )


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
have to be for this answer to fit it.\
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
    asked = axis_question or f"이 문서의 {axis}는 무엇인가?"
    return Prompt(
        system=_GROUP_ALONG_SYSTEM,
        user=in_their_language(
            f"THIS FOLDER IS DIVIDED BY: {axis}\nTHE QUESTION ITS FOLDERS ANSWER: {asked}"
            f"\n\n{user}",
            language,
        ),
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


def build_class_sign(*, shared: str, language: str = "") -> Prompt:
    """Step four: the line under the name, written without being shown the name.

    Shown it, the sign came back as the name again 22 times in 349, empty 9 more, and
    once past the note budget -- the same failure as every other step handed the string
    it must not produce. It does not need the name: a sign written from what the group
    shares is longer and more specific than two or three words can be, which is what the
    line is for.
    """
    return Prompt(
        system=_CLASS_SIGN_SYSTEM,
        user=in_their_language(f"WHAT THE DOCUMENTS IN THIS FOLDER SHARE: {shared}", language),
    )


def build_axis(*, shared: str, language: str = "") -> Prompt:
    """Step two, and only when a folder divides for the first time.

    Before the name exists, so there is no name to hand back. Asked after the name, it
    came back as the name itself in 72 of 80 replies and every division was refused.

    The documents are not shown either. With them, one root chose 시행규칙이 규정한 거래
    유형 -- an axis naming the kind of instrument, read straight off the titles -- and
    every folder underneath became an answer to it, down to a spine of 법령 및 정책
    목적별 거래 유형 holding 243 of 300 documents. The group's own sentence is the
    evidence this question needs, and the only piece already about the subject.
    """
    return Prompt(
        system=_AXIS_SYSTEM,
        user=in_their_language(f"WHAT THE PICKED DOCUMENTS SHARE: {shared}", language),
    )


class Emerging(BaseModel):
    """One class that has grown thick enough to leave the pile.

    One name, deliberately. A reply that could carry several would be a partition, and a
    partition of a heterogeneous pile always needs somewhere to put the remainder.

    The concrete candidate comes first and the verdict last, so constrained decoding
    cannot commit to either before looking at the evidence, and no free-form scratchpad
    has to be paid for.

    The axis sits between them for the same reason. Generated first, it was written
    before the model had considered what actually distinguishes these documents, and it
    came back as whichever property phrase the prompt made most salient -- the ancestor's,
    which the prompt shows in order to forbid it. One folder proposed its parent's exact
    property on every arrival from 61 documents to 68, was refused every time, and never
    divided. Named after the class, the axis answers "what property is this class a value
    of", which is a question about the class.
    """

    sign: str = Field(
        default="",
        description=(
            "One short line a reader uses to decide whether to open this folder or skip it: "
            "what kind of document belongs here. Say more than the folder name will say. "
            "Positive only: no excluded documents, no leftovers, no document ids, no "
            "description of how you decided."
        ),
    )
    name: str = Field(
        default="",
        description="Folder name for what you just described, one level. Not a path.",
    )
    axis: str = Field(
        default="",
        description=(
            "The ONE property the name you just wrote is a value of -- the question every "
            "sub-folder here will answer. Not a comparison of candidate properties and not "
            "an explanation. Asked only the first time; after that the folder already has "
            "one and you are held to it."
        ),
    )
    axis_question: str = Field(
        default="",
        description=(
            "One question about that one property. Every child folder name must be a direct "
            "answer to it. Asked only the first time."
        ),
    )
    emerged: bool = Field(description="False when the concrete candidate is not worth a shelf.")

    @field_validator("axis")
    @classmethod
    def _axis_is_a_label(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("axis must be a single-line label")
        value = " ".join(value.split()).strip()
        if value and not is_axis_label(value):
            raise ValueError("axis must be a non-empty, single-line label")
        return value


class Members(BaseModel):
    """Which documents go under one named sign. The rest are not asked about."""

    document_ids: list[str] = Field(
        default_factory=list,
        description="Only the documents that belong under this sign. The rest stay where they are.",
    )


class Group(BaseModel):
    """One sub-folder a division would create."""

    name: str = Field(description="Folder name, one level. Not a path.")
    note: str = Field(
        description=(
            "One short line positively describing only what belongs here. Do not discuss "
            "candidate axes, excluded documents, leftovers, or the classification process."
        )
    )
    document_ids: list[str] = Field(
        default_factory=list, description="The documents that go into this group."
    )


class Division(BaseModel):
    """The signs to put up, once it has been decided there should be some."""

    basis: str = Field(
        default="",
        description=(
            "The name of the one property the signs follow. Recorded on the folder and read "
            "back every time it is looked at again, so a sentence here is a sentence "
            "every later question is asked against."
        ),
    )
    basis_question: str = Field(default="")
    groups: list[Group] = Field(default_factory=list)
    reuse_existing: bool = Field(default=False, exclude=True)

    @field_validator("basis")
    @classmethod
    def _basis_is_a_label(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("basis must be a single-line label")
        value = " ".join(value.split()).strip()
        if value and not is_axis_label(value):
            raise ValueError("basis must be a non-empty, single-line label")
        return value


class ExistingAssignment(BaseModel):
    """Documents routed through one opaque existing-folder handle."""

    folder_id: str = Field(description="One shown F### direct-child ID, copied exactly.")
    document_ids: list[str] = Field(default_factory=list)

    @field_validator("folder_id")
    @classmethod
    def _normalise_folder_id(cls, value: str) -> str:
        return value.strip().rstrip("/\\").strip().strip("[]").strip().upper()


class ExistingAssignments(BaseModel):
    """Loose documents that clearly belong behind signs already present."""

    groups: list[ExistingAssignment] = Field(
        default_factory=list,
        description=(
            "Only confident assignments to shown folder IDs; unassigned documents remain "
            "where they are."
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


def in_their_language(user: str, language: str) -> str:
    """Name the collection's own language first, then show the evidence.

    It used to be the closing line, on the theory that a small model still holds the last
    instruction it read when it starts writing. Measured the other way round: six of
    thirteen replies came back in English with the line at the end, none of nine with it
    at the front. A prompt that ends in a hundred document titles already ends in the
    documents' own language, so the instruction was competing with the evidence rather
    than framing it.

    Takes the body and returns it, rather than returning a fragment to concatenate, so
    that no caller can put it back at the end. The code comes off the cards, so an
    English archive gets English back and this file names no language itself.
    """
    if not language:
        return user
    return (
        f"These documents are written in `{language}`. Write every value you return "
        f"-- the property, the question, each folder name and each sign -- in `{language}`, "
        f"using the words these documents use.\n\n{user}"
    )


def _listing(
    path: str, purpose: str, documents: list[tuple[str, str]], children: list[tuple[str, str]]
) -> str:
    # The path is at the top, but a small model reads it as an address, not a constraint.
    # Naming the enclosing folder as a settled fact is what stops the reply that proposes
    # the folder's own name back: 51 of 180 refusals in one 300-document round.
    name = PurePosixPath(path).name if path else ""
    return _LISTING.format(
        path=path or "(root)",
        already=(
            f"EVERY DOCUMENT HERE IS ALREADY {name}; NAME A KIND OF IT, NOT IT AGAIN.\n"
            if name
            else ""
        ),
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
    # Counted, because the model cannot weigh a cost it has to tally itself. One
    # 300-document round left twenty-eight signs at the root, several of them a single
    # law's title, and a reader choosing among twenty-eight names is back to scanning.
    return (
        f"EXISTING SUB-FOLDERS ({len(children)} of them, and a reader must choose one):\n{rendered}"
    )


class Grouping(BaseModel):
    """One broader shelf drawn over sign posts that already stand side by side.

    The fourth operation, and the only one that moves a folder rather than a document.
    Adding classes one at a time can only widen a level; nothing could ever narrow one
    again, so the width a folder reached early was the width it kept -- measured across
    eight rounds of 300 documents as a root of 3, then 4, then 22, decided entirely by
    how broad the first two or three classes happened to be.

    Same field order as ``Emerging`` and for the same reason: the concrete candidate is
    formed before the verdict, so constrained decoding cannot commit to "yes" before it
    has anything to say yes to.
    """

    sign: str = Field(
        default="",
        description=(
            "One short line a reader uses to decide whether to open this folder or walk "
            "past it. What the shelves inside it have in common, said positively."
        ),
    )
    name: str = Field(
        default="",
        description="Folder name for what you just described, one level. Not a path.",
    )
    emerged: bool = Field(
        default=False,
        description="False when no group of these folders belongs together under one name.",
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
        or "  (하위 폴더 없음)"
    )
    beside = (
        "\n".join(f"  {name}/" + (f" — {note}" if note else "") for name, note in siblings)
        or "  (없음)"
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
