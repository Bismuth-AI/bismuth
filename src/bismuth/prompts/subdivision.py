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

*Still right?* is what an already-divided folder is asked once the evidence has doubled,
and it is the only question that may redraw a boundary.

There is no free-form reasoning metadata in these schemas. Emerging writes its concrete
candidate before its verdict so constrained decoding does not commit before identifying
what allegedly emerged. Review returns only checks that directly decide whether the
boundary holds. After a failed review, membership-free replacement signs are designed
first and request-local document handles are assigned in separate bounded packets.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, Field, field_validator

from bismuth.domain.maintenance import is_axis_label
from bismuth.ports.llm import Prompt

_SIGNS = """\
Think of folders as SIGNS, not as groups.

Someone is looking for a document. Right now they see the list of documents in this \
folder. If it is divided they see a few folder names INSTEAD, and must choose one \
before they see any document at all. A division is worth making when those names let \
them ignore most of the collection, and it is worth nothing otherwise.

A division fails when:

- **There are nearly as many signs as documents.** Reading a second list merely to \
reach the first list adds navigation without narrowing it.
- **A sign points at one document.** A sign in front of a single book is not a sign, \
it is the book with a step in front of it.
- **Most documents fit no sign.** Then the reader is back to scanning, and the \
distinction drawn is not the one this collection is organised by.

A sign names a CLASS -- something you expect more of -- never one document's subject. \
If the only honest name for a group is the title of the document inside it, that group \
should not exist.

**One sign says one thing.** A name that strings several unrelated things together is \
not a sign, it is an inventory of what happened to land there, and a reader cannot tell \
which part of it is meant -- so they open it, which is the cost the sign exists to avoid. \
It also cannot grow: the next document either extends the list or does not belong, and \
either way the name has to be rewritten. If you can only name the group by listing its \
contents, you have not found the class. Either name the one thing they are all a case \
of, or take the thickest of them alone and leave the rest loose.

**A class that covers every document here is not a class, it is this folder.** Putting \
all of them behind one new sign moves the whole pile down a level and leaves the reader \
facing the same list one click further in; the folder below is then the same size with \
the same problem, for ever. If everything here really does belong under one name, you \
have not found the distinction yet -- look for the property that separates these \
documents from each other, not the one they share. Some of them must stay behind for a \
division to have happened at all.

**The folder you are inside already has a name, and your shelf goes underneath it.** \
Every document here is already an answer to that name, so writing it again -- or writing \
it in other words -- makes a folder that rules nothing out and a reader who has to open \
it anyway. What you name is one KIND of what this folder already holds: narrower than \
the folder's own name, and a name only SOME of these documents could take. If nothing \
narrower than the folder itself fits, nothing has emerged; say so.

**Every level costs the reader a correct guess.** Look at how deep the folder path \
above already is: a shelf you add here sits behind all of those choices, and a reader \
who guesses wrong at any one of them never reaches it. Deep down, the bar for another \
level is higher than it was at the top, not lower. A wide shelf a reader can scan beats \
a narrow one they have to find.

**The name is one folder, not a path.** Write the single level you are adding. A name \
with a separator in it is refused outright and the whole answer is thrown away.

**Name the shelf, not the sorting.** A name that describes the act of arranging rather \
than what stands on the shelf -- the leftovers, the remainder, the ones sorted by \
subject -- leaves the reader with the same list and one more click to reach it. If the \
only thing a group has in common is that it was left over, it is not a group.

**Nothing is named by what it is not.** A shelf whose name says "the ones that are not X" \
holds everything in the world except X, so it excludes nothing and can never be split \
further -- what would its children be? A reader cannot use it either: they know what they \
are looking for, not what they are not looking for. Two things produce such a name, and \
both are the same mistake. Splitting on a property where almost every document here gives \
the same answer leaves one shelf and a not-that remainder. And drawing a class you can \
only describe against the class beside it means the distinction is that other class, not \
this one. If the honest name is "the rest", nothing has emerged -- say so and leave them \
where they are.

**An attribute nearly every document has is not the first distinction to draw.** Format, \
type, language, date, and issuing body are known for almost everything, so they fill a \
tree neatly and leave the reader no better off: someone arrives wanting a subject, not a \
format. Those attributes are usable LATER, inside a shelf that is already about \
something, when there is still a reason to split it further. Drawn first, they scatter \
each subject across every branch and the reader has to walk all of them.

The first question is what the documents are ABOUT.

**`sign` is the line printed under the folder name, and `name` is the folder name.** \
They are not the same text. The reader has already read the name; the sign is what they \
read next to decide whether to open the folder or walk past it, so it has to say \
something the name did not. Write the sign first and name the folder after it.

A broad name needs a sign more, not less. `name` is two or three words and cannot carry \
scope; the sign is where the scope goes -- what kinds of documents fall under it, said in \
a full line. That is not an inventory: an inventory lists what happens to be here today, \
and scope says what would belong here tomorrow. If the sign you are about to write is the \
name again, you have written the label twice and told the reader nothing.

**The sign is ONE sentence, and it is addressed to someone standing outside the folder.** \
They cannot see the documents you are looking at and they are not interested in how you \
decided. So write what belongs here -- not what you noticed about the pile, not what these \
particular documents have in common, not why this class and not another. A sign that opens \
by talking about the documents in front of you, or that explains the distinction you drew, \
is an account of your reasoning; it goes past the length a folder note can hold, is thrown \
away, and the folder ends up labelled with its own name repeated back. Two clauses at most. \
If you cannot say it in one sentence, the class is not clear enough yet.

Signs and notes in the DOCUMENTS' OWN LANGUAGE. If the documents are in one language, the \
sign is in that language too.\
"""

_EMERGING_SYSTEM = f"""\
These documents have not been sorted, and this folder has no sub-folders yet. You are \
deciding TWO things, and the first one outlives this answer.

**First, the CLASS** -- one group that has gathered here and is worth a shelf. Describe \
it, then name it.

**Then the AXIS**: the one property that name is a value of. Every sub-folder this folder \
ever gets will be an answer to it, so it outlives this reply. Prefer the property that \
lets a reader rule out the most documents, keeps answers mutually exclusive, and stays \
meaningful as the collection grows. Return the name of ONE property, not a comparison \
between candidates and not an explanation. Do not use any domain rule that is not \
evidenced by the documents.

**The axis you choose here is permanent.** Every later question about this folder is asked \
against it, and nothing after this can change it. You are choosing it from the documents \
in front of you now, which may be a small and unrepresentative part of what this folder \
will eventually hold.

So look at what these documents do NOT have in common. A property they all share the same \
value on cannot divide anything -- if the only thing two documents have in common is that \
they are the same kind of document, "kind of document" is what they look like, not what \
this collection is organised by. Choosing it now would fix it forever on the evidence of \
a handful of files.

**And a property they all answer DIFFERENTLY divides just as badly.** A name, a title, a \
number, an identifier: everything has one and no two are alike, so every folder ends up \
holding one document and the reader is back to reading the list, now with a step in front \
of every entry. The property you want is the one where a handful of answers cover \
everything here -- several documents sharing each answer, and no document left without \
one.

If you cannot yet see which property will still matter when this folder is ten times \
larger, say so: `emerged` is false, nothing is created, and you will be asked again with \
every new document. Waiting costs one more question. Choosing wrong costs the archive.

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
different is a list, and a list is best left as a list. Say so.\
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

_REVIEW_SYSTEM = f"""\
You are re-examining a folder you divided earlier. You are told what distinction it \
was divided along and how many documents there were at the time. There are more now. \
This request is one isolated evidence packet. Across packets the application presents \
every document and every direct sign, then combines the checks fail-closed. Judge only \
whether this packet supplies evidence that the current boundary fails; do not assume \
that documents or signs absent from this packet do not exist.

**Your default answer is that the existing division still holds.** It was made by \
someone looking at the same archive, and moving documents has a cost paid by every \
person who had learned where things were. Overturn it only if you can say what is \
actually wrong -- a sign that no longer means anything, a distinction that has turned \
out to cut across the real one, documents that have gathered and clearly belong \
together elsewhere.

{_SIGNS}

**A boundary is never finished, and unfinished is not the same as wrong.** Shelves appear \
one at a time as classes gather, so at any moment some documents sit in the folder itself \
with no shelf yet -- their `current` path is the bare filename. That is the designed \
state, not a defect. You are asked whether what has been built is RIGHT, not whether it \
is COMPLETE.

Saying no here does not add the missing shelves. It throws away the ones that exist and \
rebuilds the whole subtree from scratch, moving every document, including the ones that \
are correctly placed today. Say no only when keeping what is there would be worse than \
that.

Judge the current boundary only. Do not propose a replacement, enumerate documents, \
recount memberships, or explain your work. Each output boolean is a directly used check; \
the application derives whether the boundary holds from all of them. If no current axis \
question was recorded, `one_axis` is false because the sibling contract is incomplete.\
"""

_LISTING = """\
FOLDER: {path}
{already}{purpose}
DOCUMENTS SITTING DIRECTLY HERE ({count}):
{documents}
{children}\
"""

_REVIEW_USER = """\
FOLDER: {path}
{purpose}
DIVIDED ALONG: {basis}
CURRENT AXIS QUESTION: {basis_question}
AT THE TIME THERE WERE {before} DOCUMENTS; THERE ARE NOW {count}.

CURRENT SUBTREE FOLDERS:
{children}

DOCUMENT EVIDENCE IN THIS ISOLATED PACKET ({loose}):
{documents}\
"""


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
    replace_existing: bool = Field(default=False, exclude=True)
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


class Review(BaseModel):
    """Directly used checks of whether the current boundary still holds."""

    one_axis: bool = Field(
        description="All direct child signs still answer the one recorded axis question."
    )
    coherent_membership: bool = Field(
        description=(
            "Documents that are behind a sign are described by it. Documents still loose in "
            "the folder itself are expected and are not counted against this."
        )
    )
    useful_navigation: bool = Field(
        description=(
            "Each sign that exists is specific enough that a reader can tell from it alone "
            "whether to open that folder. Judge the signs as written, not how much of the "
            "folder they cover: shelves this boundary has not grown yet are not a fault in "
            "the shelves it has."
        )
    )

    @property
    def holds(self) -> bool:
        return self.one_axis and self.coherent_membership and self.useful_navigation


class Replacement(BaseModel):
    """A complete new boundary, requested only after the current one fails."""

    basis: str = Field(
        default="",
        description=(
            "The name of the ONE property used by the complete replacement. Not a "
            "comparison between candidate properties and not an explanation."
        ),
    )
    basis_question: str = Field(
        default="",
        description="One question about one property that every replacement group answers.",
    )
    groups: list[Group] = Field(default_factory=list)

    @field_validator("basis")
    @classmethod
    def _basis_is_a_label(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("basis must be a single-line label")
        value = " ".join(value.split()).strip()
        if value and not is_axis_label(value):
            raise ValueError("basis must be a non-empty, single-line label")
        return value


class ReplacementSign(BaseModel):
    """One sign in a context-bounded replacement sketch; membership comes later.

    ``name`` carries no length ceiling on purpose. It had ``max_length=120`` and a real
    run returned a median name length of exactly 120 -- the budget was read as an
    instruction and saturated, and 83% of the names were then silently cut to fit a
    64-character path segment. The same model, in the same run, produced a median of 10
    characters for ``Emerging.name``, which says what a folder name is and stops.
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
    name: str = Field(description="Folder name for what you just described, one level. Not a path.")


class ReplacementSketch(BaseModel):
    """A boundary design with no document IDs, safe to reduce across evidence packets."""

    # No character ceilings here either, for the reason on ReplacementSign: a field
    # budget is read as an instruction. Generation is bounded by the schema's output cap,
    # which is a transport circuit breaker and never fired in the run that produced the
    # saturated names.
    basis: str = Field(description="The name of the one property used by every sign.")
    basis_question: str = Field(description="One question every sign name directly answers.")
    signs: list[ReplacementSign] = Field(min_length=2, max_length=12)

    @field_validator("basis")
    @classmethod
    def _basis_is_a_label(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("basis must be a single-line label")
        value = " ".join(value.split()).strip()
        if not is_axis_label(value):
            raise ValueError("basis must be a non-empty, single-line label")
        return value


class ReplacementAssignment(BaseModel):
    """Membership for one displayed replacement-sign handle."""

    folder_id: str = Field(description="One shown G### sign ID, copied exactly.")
    document_ids: list[str] = Field(default_factory=list)

    @field_validator("folder_id")
    @classmethod
    def _normalise_folder_id(cls, value: str) -> str:
        return value.strip().rstrip("/\\").strip().strip("[]").strip().upper()


class ReplacementAssignments(BaseModel):
    """Complete membership for one bounded document packet."""

    groups: list[ReplacementAssignment] = Field(default_factory=list)
    unassigned_document_ids: list[str] = Field(
        default_factory=list,
        description="Documents that genuinely fit no proposed sign; any make the plan abort.",
    )


class BoundaryAudit(BaseModel):
    """Independent semantic check before a proposed boundary reaches the filesystem."""

    one_property: bool = Field(
        description="The axis names one property, not a comparison or mixture of properties."
    )
    names_answer_question: bool = Field(
        description="Every proposed folder name is a direct answer to the axis question."
    )
    mutually_exclusive: bool = Field(
        description="A document does not naturally belong to several proposed sibling classes."
    )
    useful_for_navigation: bool = Field(
        description="The signs materially narrow the search instead of restating the documents."
    )
    each_name_is_one_answer: bool = Field(
        default=True,
        description=(
            "No proposed name offers a choice between two answers instead of being one of "
            "them. A name that joins two alternatives holds both, so it excludes nothing."
        ),
    )
    subject_before_attribute: bool = Field(
        default=True,
        description=(
            "This boundary is not the first cut on an attribute nearly every document has "
            "-- format, type, language, date, issuing body. Such a boundary is well formed "
            "and still leaves a reader who arrived with a subject no better off. True when "
            "the axis is about what the documents are ABOUT, or when the folder is already "
            "narrowed by subject and this attribute is a reasonable further split."
        ),
    )

    @property
    def accepted(self) -> bool:
        return all(
            (
                self.one_property,
                self.names_answer_question,
                self.mutually_exclusive,
                self.each_name_is_one_answer,
                self.subject_before_attribute,
                self.useful_for_navigation,
            )
        )


class ReplacementAudit(BaseModel):
    """Whether a replacement fixes the failed boundary and improves navigation."""

    fixes_observed_failure: bool = Field(
        description="The proposal directly fixes the failure found in the current boundary."
    )
    better_navigation: bool = Field(
        description="The proposed signs narrow search materially better than the current signs."
    )

    @property
    def accepted(self) -> bool:
        return self.fixes_observed_failure and self.better_navigation


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


class RoutingAudit(BaseModel):
    """Independent check that a partial refiling follows the existing boundary."""

    assignments_match_signs: bool = Field(
        description="Each document clearly belongs under the existing sign named for it."
    )
    no_forced_fit: bool = Field(
        description="No document is assigned merely because one existing sign is closest."
    )

    @property
    def accepted(self) -> bool:
        return self.assignments_match_signs and self.no_forced_fit


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
        return Prompt(system=_EMERGING_SYSTEM, user=user + answer_in(language))
    return Prompt(
        system=_EMERGING_ALONG_SYSTEM,
        user=f"{user}\n\nTHE AXIS HERE: {axis}{answer_in(language)}",
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


def build_emerging_reduce(
    *,
    path: str,
    purpose: str,
    axis: str,
    children: list[tuple[str, str]],
    candidates: list[Emerging],
) -> Prompt:
    """Choose one class from candidates discovered in isolated document packets."""
    rendered = "\n".join(
        f"  - axis={candidate.axis or axis} | question={candidate.axis_question} | "
        f"name={candidate.name}"
        for candidate in candidates
        if candidate.emerged
    )
    existing = _render_children(children) or "  (none)"
    axis_rule = (
        f"The existing axis is {axis!r}; the selected candidate must stay on it."
        if axis
        else "Select one property shared by every future sibling."
    )
    return Prompt(
        system=(
            "Document packets independently proposed classes that may have emerged in one "
            "library folder. Select the strongest reusable class, resolving synonyms and "
            "discarding packet-local or document-title shelves. Return one candidate only, "
            "with no document IDs or explanation. " + axis_rule
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nPURPOSE: {purpose or '(none)'}\n"
            f"EXISTING SIGNS:\n{existing}\n\nPACKET CANDIDATES:\n{rendered}"
        ),
    )


REVIEW_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "one_axis",
        "FAILS if some sub-folder names answer a different question from the one recorded "
        "above, so the names sit on two different kinds of distinction, or if no axis "
        "question was recorded at all.\n"
        "HOLDS if every sub-folder name answers that one recorded question.",
    ),
    (
        "coherent_membership",
        "Consider only the documents that are inside a sub-folder. Documents still loose in "
        "this folder are inside no sub-folder and are not part of this question.\n"
        "FAILS if documents sit behind signs that do not describe them.\n"
        "HOLDS if the documents behind each sign are described by it.",
    ),
    (
        "useful_navigation",
        "Read the sub-folder signs as a reader who wants one document. Shelves that do not "
        "exist yet are not part of this question.\n"
        "FAILS if a sign is so vague, or so overlapping with another, that the reader would "
        "have to open both.\n"
        "HOLDS if each sign is specific enough to decide from the sign alone whether to open "
        "that folder.",
    ),
)
"""One check per call. Three booleans in one reply came back all-false on boundaries whose
signs were specific and correct; asked one at a time, against the same evidence, each has
a single thing to weigh. This is the SPEC 2.1 contract applied to the one question that
can destroy structure."""

_REVIEW_CHECK_SYSTEM = """\
You are re-examining a library folder that was divided earlier, and you are checking ONE \
thing about it. Answer with exactly HOLDS or FAILS and nothing else.

**HOLDS is the default.** The division was made by someone looking at this same archive, \
and undoing it moves every document, including the ones that are already in the right \
place. Answer FAILS only when you can point at what is actually wrong.

A boundary is never finished. Shelves appear one at a time as classes gather, so some \
documents always sit in the folder itself with no shelf yet -- their `current` path is the \
bare filename. That is the designed state. You are asked whether what has been built is \
RIGHT, not whether it is COMPLETE.

THE ONE CHECK:
{check}\
"""


def build_review_check(
    *,
    check: str,
    path: str,
    purpose: str,
    basis: str,
    basis_question: str,
    before: int,
    count: int,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
) -> Prompt:
    """One closed HOLDS/FAILS question about an existing division."""
    return Prompt(
        system=_REVIEW_CHECK_SYSTEM.format(check=check),
        user=_review_listing(
            path=path,
            purpose=purpose,
            basis=basis,
            basis_question=basis_question,
            before=before,
            count=count,
            documents=documents,
            children=children,
        ),
    )


def build_review(
    *,
    path: str,
    purpose: str,
    basis: str,
    basis_question: str,
    before: int,
    count: int,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
) -> Prompt:
    """Ask whether an existing division still holds."""
    return Prompt(
        system=_REVIEW_SYSTEM,
        user=_review_listing(
            path=path,
            purpose=purpose,
            basis=basis,
            basis_question=basis_question,
            before=before,
            count=count,
            documents=documents,
            children=children,
        ),
    )


def build_replacement_sketch(
    *,
    path: str,
    purpose: str,
    current_axis: str,
    current_question: str,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
    language: str = "",
) -> Prompt:
    """Propose signs from one bounded packet; document membership is assigned later."""
    return Prompt(
        system=(
            "The current library boundary failed review. Design a replacement boundary from "
            "this bounded evidence packet. This is one packet from a larger subtree, so do not "
            "return document IDs. Name one corpus-evidenced property and two or more reusable "
            "class signs on that property. Signs must be mutually exclusive, useful for ruling "
            "alternatives out, and written in the documents' own language. Each class has two "
            "separate texts and they are never the same text: `name` is the folder name, and "
            "`sign` is the line printed under it, which has to say something the name did not. "
            "Write the sign first and name the folder after it. Signs are short positive routing "
            "rules, not inventories, counts, exclusions, or process narration. "
            "Do not import a domain taxonomy that is absent from the evidence."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nPURPOSE: {purpose or '(none)'}\n"
            f"FAILED AXIS: {current_axis}\nFAILED QUESTION: {current_question}\n"
            f"CURRENT DIRECT SIGNS:\n{_render_children(children) or '  (none)'}\n\n"
            f"EVIDENCE PACKET ({len(documents)} documents):\n{_render_documents(documents)}"
        ),
    )


def build_replacement_reduce(*, path: str, sketches: list[ReplacementSketch]) -> Prompt:
    """Reduce several isolated packet sketches into one coherent boundary design."""
    rendered = "\n\n".join(
        f"CANDIDATE {index}:\n  AXIS: {sketch.basis}\n  QUESTION: {sketch.basis_question}\n"
        + "\n".join(f"  - {sign.name}/" for sign in sketch.signs)
        for index, sketch in enumerate(sketches, start=1)
    )
    return Prompt(
        system=(
            "Consolidate candidate library boundaries produced from separate evidence packets. "
            "Return one boundary on one property whose signs can cover the classes evidenced "
            "across all candidates. Resolve synonyms and competing axes; do not mix axes or add "
            "a remainder class. Use the archive's own language. Return no document IDs and "
            "no explanation."
        ),
        user=f"FOLDER: {path or '(root)'}\n\nPACKET CANDIDATES:\n{rendered}",
    )


def build_replacement_assignments(
    *,
    path: str,
    documents: list[tuple[str, str]],
    sketch: ReplacementSketch,
) -> Prompt:
    """Assign one bounded packet completely after the global signs are fixed."""
    signs = "\n".join(
        f"  [G{index:03d}] {sign.name}/" for index, sign in enumerate(sketch.signs, start=1)
    )
    return Prompt(
        system=(
            "Assign every shown document to exactly one proposed library sign. Copy only G### "
            "handles and D#### document handles exactly. Do not rename or create signs. If a "
            "document genuinely fits no sign, put its ID in unassigned_document_ids; never force "
            "the nearest fit. Return no explanation."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {sketch.basis}\n"
            f"QUESTION: {sketch.basis_question}\nSIGNS:\n{signs}\n\n"
            f"DOCUMENT PACKET ({len(documents)}):\n{_render_documents(documents)}"
        ),
    )


def build_existing_choice(
    *,
    path: str,
    document: tuple[str, str],
    axis: str,
    axis_question: str,
    children: list[tuple[str, str]],
) -> Prompt:
    """Route one loose document with a closed, non-JSON decision."""
    signs = "\n".join(
        f"  [F{index:03d}] {name}/" for index, (name, _) in enumerate(children, start=1)
    )
    return Prompt(
        system=(
            "Route this one loose document only when an existing sign positively describes it. "
            "Reply with exactly one shown F### handle or STAY. STAY is normal when no sign is a "
            "clear fit. Do not explain, rename, or create a sign."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"SIGNS:\n{signs}\n\nDOCUMENT:\n  [{document[0]}] {document[1]}"
        ),
    )


def build_member_choice(*, path: str, purpose: str, document: tuple[str, str], name: str) -> Prompt:
    """Decide membership in one new class without echoing a document ID."""
    return Prompt(
        system=(
            "A new library class has been named. Decide whether this one document genuinely "
            "belongs behind that sign. Reply with exactly SHELF or STAY. Do not explain. STAY "
            "means the document remains safely in its current folder."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nPURPOSE: {purpose or '(none)'}\n"
            f"NEW SIGN: {name}/\n\nDOCUMENT:\n  [{document[0]}] {document[1]}"
        ),
    )


def build_replacement_choice(
    *, path: str, document: tuple[str, str], sketch: ReplacementSketch
) -> Prompt:
    """Assign one document to one fixed replacement sign, or leave it where it is.

    ``STAY`` is the whole point. Offered only the signs, a model shown a document about
    something else has to pick one anyway -- and it picks whichever name sounds broadest.
    Measured on 300 documents: a root redrawn on 금융거래 및 금융기관 감독 pulled 102
    documents into six finance shelves, and 중대재해처벌법, 과학기술기본법 and
    국립공업고등학교 설치령 landed behind 금융산업 구조개선, which ended up holding 51
    documents of which four were about it. The residue was manufactured by the question.
    """
    signs = "\n".join(
        f"  [G{index:03d}] {sign.name} — {sign.sign}" for index, sign in enumerate(sketch.signs, 1)
    )
    return Prompt(
        system=(
            "A replacement boundary has already been fixed. Say where this one document "
            "goes. Reply with exactly one G### handle, or STAY. Do not explain, rename, or "
            "create a sign.\n\n"
            "Choose a sign only when it positively describes this document. STAY when none "
            "of them does -- the document keeps the folder it is in now, which is a normal "
            "and safe outcome. Never choose the closest sign merely because the document "
            "has to go somewhere: a document filed under a name that does not describe it "
            "is worse than one left where it was, because the name then lies to every "
            "reader who trusts it."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {sketch.basis}\n"
            f"QUESTION: {sketch.basis_question}\nSIGNS:\n{signs}\n\n"
            f"DOCUMENT:\n  [{document[0]}] {document[1]}"
        ),
    )


BOUNDARY_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "one_property",
        "FAILS if the axis names two properties, compares candidate properties, or is an "
        "explanation rather than the name of a property.\n"
        "HOLDS if the axis is the name of one property.",
    ),
    (
        "names_answer_question",
        "FAILS if a proposed name answers some other question, or is not an answer at all.\n"
        "HOLDS if every name listed under GROUPS is an answer to the axis question.",
    ),
    (
        "mutually_exclusive",
        "FAILS if two proposed names overlap so a reader could not tell which to open, or if "
        "a single proposed name would equally well describe the documents staying behind.\n"
        "HOLDS if each proposed name claims a distinct part of this folder. One name alone "
        "has no sibling to overlap with, so it normally HOLDS.",
    ),
    (
        "useful_for_navigation",
        "FAILS if the name is so vague or so general that a reader looking for something "
        "else would have to open it anyway.\n"
        "HOLDS if a reader who wants something else can see this name and skip it. Documents "
        "left loose in the folder are expected and are not a fault in the name.",
    ),
    (
        "each_name_is_one_answer",
        "FAILS if a name joins two alternatives, because such a folder holds both and "
        "excludes nothing.\n"
        "HOLDS if each name is a single answer.",
    ),
    (
        "subject_before_attribute",
        "Some properties are known for nearly every document -- what kind of document it is, "
        "what form it takes, who issued it, when, in what language. A tree built on one of "
        "those FIRST is well formed and still useless to a reader who arrived looking for a "
        "topic, because every topic is then spread across every folder.\n"
        "FAILS if FOLDER is the root or an undivided folder AND the axis is one of those "
        "always-present properties.\n"
        "HOLDS if the axis is about what the documents are ABOUT. Also HOLDS if this folder "
        "is already narrowed by topic and the property is a sensible way to split what is "
        "left.",
    ),
)
"""One check per call, each ending on the condition that HOLDS.

Three phrasings were measured. Six booleans in one reply approved the axes 문서의 성격,
주관 부처 and 법령의 성격 -- the three this exists to reject. Split into closed calls but
phrased as questions, three checks failed nearly everything. Split and phrased as a
statement followed by "FAILS if ...", five of six failed nearly everything and the archive
stayed flat at thirty documents: the answer was tracking the last condition read, which is
the same recency this repository already handles by generating a verdict field last."""


_AXIS_CHECK_SYSTEM = """\
A library folder is about to be divided, and you are checking ONE thing: whether the \
property it is being divided on is a good one to divide on HERE. Answer with exactly \
FAILS or HOLDS and nothing else.

Some properties are known for nearly every document -- what kind of document it is, what \
form it takes, who issued it, when it was issued, what language it is in. Sorting by one \
of those produces a tidy tree and a useless one: a reader who arrives wanting a subject \
finds that every subject has been spread evenly across every folder, so they must open \
all of them. The tree looks organised and narrows nothing.

Sorting by what the documents are ABOUT does the opposite. A reader who wants one \
subject opens one folder.

FAILS if this folder is the root, or has no boundary yet, and the property is one of \
those always-present ones -- the kind of document, its form, its issuer, its date, its \
language.

FAILS if what is offered is not the NAME of a property at all: a sentence describing the \
split, a comparison between two candidates, or an explanation of why it was chosen. A \
property is named in a few words, the way a column heading is.

FAILS if the property is one the folders ABOVE are already divided on. Those are listed. \
Every document here already has the same answer to them, so dividing on one again \
separates nothing and only restates the parent's name in other words.

Sharing a WORD with an ancestor's property is not the same thing. 규제 대상 산업 under a \
parent divided on 규제 대상 및 목적 is a different question, and it HOLDS. The test is \
whether the documents in front of you would give different answers to it -- not whether \
it reads like something above.

FAILS if almost every document here would give the SAME answer to it. That draws one \
real shelf and a remainder nobody can name except as "the ones that are not that" -- a \
folder holding everything except one thing, which excludes nothing and cannot be divided \
again. Observed live: inside a folder already about 과학기술, dividing on which ministry \
issued the documents produced one shelf and 비과학기술 분야 소관 beside it.

HOLDS if the property is about what the documents are about. HOLDS also when this \
folder has already been narrowed by subject and the property is a sensible way to split \
what remains -- but only if the documents here really do spread across several of its \
answers. Standing inside a subject licenses a different question, not one whose answer \
is already fixed for nearly everything in the folder.\
"""


def build_axis_check(
    *, path: str, axis: str, axis_question: str, names: list[str], spent: list[str] | None = None
) -> Prompt:
    """One closed question about the property a boundary is being drawn on.

    Separated from the rest of the audit deliberately. Asked as one of six booleans in a
    single reply, this check approved 문서의 성격, 주관 부처 and 법령의 성격 -- every axis
    it exists to reject. Asked on its own it has one thing to weigh. Splitting all six
    was measured three times and was worse each time, because most of the others ask
    about sibling names that do not exist yet when a single class is drawn out; this one
    is about the property alone, which is present either way.
    """
    return Prompt(
        system=_AXIS_CHECK_SYSTEM,
        user=(
            f"FOLDER: {path or '(root)'}\n"
            "PROPERTIES THE FOLDERS ABOVE ARE ALREADY DIVIDED ON:\n"
            + ("\n".join(f"  {item}" for item in spent) if spent else "  (none)")
            + f"\n\nPROPERTY: {axis}\nQUESTION IT ASKS: {axis_question}\n"
            f"FOLDER NAMES IT WOULD PRODUCE: {', '.join(names)}"
        ),
    )


_BOUNDARY_CHECK_SYSTEM = """\
You are the independent verifier for a proposed library folder boundary, and you are \
checking ONE thing about it. Answer with exactly HOLDS or FAILS and nothing else.

Judge only from the documents and the proposal you are shown. Do not bring in a \
preferred way of organising this kind of material. Folder notes are written by the \
application and are not part of your judgement.

THE ONE CHECK:
{check}\
"""


def build_boundary_check(
    *,
    check: str,
    path: str,
    documents: list[tuple[str, str]],
    axis: str,
    axis_question: str,
    groups: list[Group],
    complete: bool,
) -> Prompt:
    """One closed HOLDS/FAILS question about a proposed boundary."""
    rendered_groups = "\n".join(
        f"  {group.name}/ — ids: {', '.join(group.document_ids)}" for group in groups
    )
    mode = (
        "This redraws the whole boundary. Documents that fit none of the new signs stay "
        "loose in the folder, so a document missing from the groups is not a fault."
        if complete
        else "This draws out one class; unclaimed documents intentionally remain loose."
    )
    return Prompt(
        system=_BOUNDARY_CHECK_SYSTEM.format(check=check),
        user=(
            f"FOLDER: {path or '(root)'}\nMODE: {mode}\nAXIS: {axis}\n"
            f"QUESTION: {axis_question}\nGROUPS:\n{rendered_groups}\n\n"
            f"DOCUMENTS:\n{_render_documents(documents)}"
        ),
    )


def build_boundary_audit(
    *,
    path: str,
    documents: list[tuple[str, str]],
    axis: str,
    axis_question: str,
    groups: list[Group],
    complete: bool,
) -> Prompt:
    """Verify semantics without supplying a domain taxonomy or preferred axis."""
    rendered_groups = "\n".join(
        f"  {group.name}/ — {group.note} — ids: {', '.join(group.document_ids)}" for group in groups
    )
    mode = (
        "This redraws the whole boundary. Documents that fit none of the new signs stay "
        "loose in the folder, so a document missing from the groups is not a fault."
        if complete
        else "This draws out one class; unclaimed documents intentionally remain loose."
    )
    return Prompt(
        system=(
            "You are the independent verifier for a proposed library boundary. Judge only "
            "from the supplied documents and proposal. Do not introduce a preferred domain "
            "taxonomy. The axis must name one property, its question must ask only that "
            "property, and every sibling name must be an answer to that question. Reject "
            "candidate comparisons, mixed axes, overlapping siblings, document-title shelves, "
            "and distinctions that do not help a reader rule alternatives out.\n\n"
            "`each_name_is_one_answer` is false when a name offers a CHOICE between two "
            "answers rather than being one of them; such a folder holds both and excludes "
            "nothing.\n\n"
            "`subject_before_attribute` is about WHICH property was chosen. Format, document "
            "type, language, date and issuing body are known for nearly every document, so a "
            "boundary on one of them is well formed and still leaves a reader who arrived "
            "with a subject no better off -- their subject is now scattered across every "
            "branch. It is false when this is the FIRST cut and it is on such an attribute. "
            "It is true when the axis is about what the documents are ABOUT, and also true "
            "when this folder is already narrowed by subject and the attribute is a "
            "reasonable further split inside it.\n\n"
            "Folder notes are derived by the application and are not part of this judgement."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nMODE: {mode}\nAXIS: {axis}\n"
            f"QUESTION: {axis_question}\nGROUPS:\n{rendered_groups}\n\n"
            f"DOCUMENTS:\n{_render_documents(documents)}"
        ),
    )


def build_replacement_audit(
    *,
    path: str,
    documents: list[tuple[str, str]],
    current_axis: str,
    current_question: str,
    current_children: list[tuple[str, str]],
    observed_failures: list[str],
    proposed_axis: str,
    proposed_question: str,
    proposed_groups: list[Group],
) -> Prompt:
    """Compare a valid replacement with the boundary readers already learned."""
    old = _render_children(current_children) or "  (none)"
    new = "\n".join(
        f"  {group.name}/ — {group.note} — ids: {', '.join(group.document_ids)}"
        for group in proposed_groups
    )
    return Prompt(
        system=(
            "You are the final change-control reviewer for a library boundary. The proposed "
            "boundary is already structurally valid; decide whether replacing the current "
            "one is materially better. Existing locations have learned value and moving them "
            "has a cost. Another plausible taxonomy is not enough. Approve only when the new "
            "boundary directly fixes the observed failure and narrows search substantially "
            "better. Those two checks already express whether disruption is justified; do not "
            "add a separate preference for preserving a boundary that has failed. Do not prefer "
            "any domain taxonomy."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nCURRENT AXIS: {current_axis}\n"
            f"CURRENT QUESTION: {current_question}\n"
            f"OBSERVED FAILURES: {', '.join(observed_failures) or '(none)'}\n"
            f"CURRENT SIGNS:\n{old}\n\n"
            f"PROPOSED AXIS: {proposed_axis}\nPROPOSED QUESTION: {proposed_question}\n"
            f"PROPOSED SIGNS:\n{new}\n\nDOCUMENTS:\n{_render_documents(documents)}"
        ),
    )


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


def build_routing_audit(
    *,
    path: str,
    documents: list[tuple[str, str]],
    axis: str,
    axis_question: str,
    groups: list[Group],
    children: list[tuple[str, str]],
) -> Prompt:
    """Verify a proposed partial refiling without inventing a taxonomy."""
    assignments = "\n".join(
        f"  {group.name}/ <- {', '.join(group.document_ids)}" for group in groups
    )
    return Prompt(
        system=(
            "Independently verify a partial refiling into existing library signs. Judge only "
            "from the supplied evidence and recorded axis. Reject forced closest-match filing, "
            "mixed-axis reasoning, and assignments not directly described by the target sign. "
            "Do not require every loose document to be assigned."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"EXISTING DIRECT SUB-FOLDERS:\n{_render_children(children) or '  (none)'}\n"
            f"PROPOSED ASSIGNMENTS:\n{assignments}\n\n"
            f"LOOSE DOCUMENTS:\n{_render_documents(documents)}"
        ),
    )


def answer_in(language: str) -> str:
    """A closing line naming the collection's own language, when it has one.

    Last, because that is the instruction a small model is most likely to still be
    holding when it starts writing. The code comes off the cards, so an English archive
    gets English back and this file names no language itself.
    """
    if not language:
        return ""
    return (
        f"\n\nThese documents are written in `{language}`. Write every value you return "
        f"-- the property, the question, each folder name and each sign -- in `{language}`, "
        "using the words these documents use."
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


def _review_listing(
    *,
    path: str,
    purpose: str,
    basis: str,
    basis_question: str,
    before: int,
    count: int,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
) -> str:
    return _REVIEW_USER.format(
        path=path or "(root)",
        purpose=f"NOTE: {purpose}\n" if purpose else "",
        basis=basis,
        basis_question=basis_question or "(not recorded)",
        before=before,
        count=count,
        loose=len(documents),
        documents=_render_documents(documents),
        children=_render_children(children) or "  (none)",
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
        user=(
            f"FOLDER: {path or '(root)'}\n"
            f"THE QUESTION THESE SUB-FOLDERS ANSWER: {axis or '(none recorded)'}\n"
            f"SUB-FOLDERS STANDING HERE ({len(children)} of them, all read before any "
            f"document):\n{rendered}" + answer_in(language)
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
