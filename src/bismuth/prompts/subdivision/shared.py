"""What every prompt here shares: the rules a folder name is held to, and the shapes.

``_SIGNS`` is embedded in five system prompts and sent on every subdivision call, so a
paragraph in it costs five times its length.

No prompt in this package carries an example from any one collection. SPEC.md 2 forbids
teaching classification by few-shot, because results lean toward whatever domain the
examples came from, and docs/spec/subdivision.md 9 says where the evidence goes instead:
the operational prompt gets the rule, the history gets the case that produced it.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, Field, field_validator

from bismuth.domain.maintenance import is_axis_label

"""What a folder name has to do, shared by every prompt that asks for one.

No prompt in this module carries an example from any one collection. SPEC.md 2 forbids
teaching classification by few-shot, because results lean toward whatever domain the
examples came from, and docs/spec/subdivision.md 9 says where the evidence goes instead:
the operational prompt gets the rule, the history gets the case that produced it.

Twenty-one such examples were removed from six of these prompts. They named the statutes,
ministries and instrument types of one 300-document legal corpus -- so a folder of photographs
or of source code was being told, in every subdivision call, what a legal archive looks like.
Every rule they illustrated stands without them.

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
        max_length=300,
        description=(
            "ONE SENTENCE, not a title and not a list: what a further document would "
            "have to be ABOUT to belong with these. It must still be true of a document "
            "that is not part of the same law, work or series. In the documents' own "
            "language, and at most about thirty words -- one reply ran the same handful "
            "of nouns to the generation limit and the whole call was thrown away."
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
