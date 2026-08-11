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
boundary holds. A complete replacement plan is requested in a second call only after one
of those checks fails.
"""

from __future__ import annotations

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

Signs and notes in the DOCUMENTS' OWN LANGUAGE.\
"""

_EMERGING_SYSTEM = f"""\
These documents have not been sorted, and this folder has no sub-folders yet. You are \
deciding TWO things, and the first one outlives this answer.

**First, the AXIS.** Every sub-folder this folder ever gets will be one answer to a \
single question, and you are choosing that question now. Consider two or three candidate \
axes before choosing. Prefer the one that lets a reader rule out the most documents, \
keeps answers mutually exclusive, avoids repeating the same split under every child, and \
is likely to remain meaningful as the collection grows. Return the name of ONE property, \
not a comparison between candidate properties and not an explanation. Do not use any \
domain rule that is not evidenced by the documents.

**Then, the first CLASS on it** -- the one value of that axis that has gathered enough \
documents to be worth a shelf.

{_SIGNS}

**You are not dividing this folder and you are not accounting for every document.** \
Whatever does not belong to the class you name stays exactly where it is. Most of these \
documents staying put is the normal outcome, not a failure.

If two classes have grown, name the thicker; you will be asked again and the other can \
come out then.

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

Judge the current boundary only. Do not propose a replacement, enumerate documents, \
recount memberships, or explain your work. Each output boolean is a directly used check; \
the application derives whether the boundary holds from all of them. If no current axis \
question was recorded, `one_axis` is false because the sibling contract is incomplete.\
"""

_REPLACEMENT_SYSTEM = f"""\
The existing boundary has failed an independent structural review. Propose a COMPLETE \
replacement using every listed document exactly once. A group name may exactly reuse an \
existing direct sub-folder or name a new one. Anything not named is retired.

{_SIGNS}

The replacement axis names ONE property, its question asks only that property, and every \
group name directly answers the question. Never compare candidate axes. Return only the \
structured plan: do not narrate analysis, enumerate documents outside `document_ids`, or \
recount memberships. The application validates completeness mechanically.\
"""

_LISTING = """\
FOLDER: {path}
{purpose}
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

    The verdict comes last. The model must first form the best concrete candidate, which
    avoids committing to a boolean before considering the evidence without paying for a
    free-form scratchpad.
    """

    axis: str = Field(
        default="",
        description=(
            "The name of the ONE property the sub-folders tell apart. It is not a "
            "comparison of candidate properties and not an explanation. Asked only the first time; "
            "after that the folder already has one and you are held to it."
        ),
    )
    axis_question: str = Field(
        default="",
        description=(
            "One question about one property. Every child folder name must be a direct "
            "answer to it. Asked only the first time."
        ),
    )
    name: str = Field(default="", description="Folder name for that class, one level. Not a path.")
    note: str = Field(
        default="",
        description=(
            "One short line positively describing only what belongs here. Do not discuss "
            "candidate axes, excluded documents, leftovers, or the classification process."
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
        description="Documents generally sit behind signs that accurately describe them."
    )
    useful_navigation: bool = Field(
        description="The current signs still help a reader rule alternatives out."
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
    """One sign in a context-bounded replacement sketch; membership comes later."""

    name: str = Field(description="Folder name, one level. Not a path.")
    note: str = Field(description="One short positive routing sign for this class.")


class ReplacementSketch(BaseModel):
    """A boundary design with no document IDs, safe to reduce across evidence packets."""

    basis: str = Field(description="The name of the one property used by every sign.")
    basis_question: str = Field(description="One question every sign name directly answers.")
    signs: list[ReplacementSign] = Field(min_length=2)

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
    notes_are_routing_signs: bool = Field(
        description=(
            "Every note positively describes only what belongs behind its sign, without "
            "axis comparison, process narration, leftovers, or excluded documents."
        )
    )

    @property
    def accepted(self) -> bool:
        return all(
            (
                self.one_property,
                self.names_answer_question,
                self.mutually_exclusive,
                self.useful_for_navigation,
                self.notes_are_routing_signs,
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
) -> Prompt:
    """Step one: has any one class grown thick enough to come out?

    With an ``axis``, the folder has been divided before and the question narrows to
    "another answer to the same question?". Without one, the axis is chosen here and
    every sub-folder this folder ever gets is held to it.
    """
    user = _listing(path, purpose, documents, children)
    if not axis:
        if spent:
            user += (
                "\n\nPROPERTIES ALREADY USED ABOVE THIS FOLDER (do not reuse them here):\n  "
                + "\n  ".join(spent)
            )
        return Prompt(system=_EMERGING_SYSTEM, user=user)
    return Prompt(system=_EMERGING_ALONG_SYSTEM, user=f"{user}\n\nTHE AXIS HERE: {axis}")


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
        f"name={candidate.name} | sign={candidate.note}"
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


def build_replacement(
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
    """Ask for a complete plan only after the current boundary fails."""
    return Prompt(
        system=_REPLACEMENT_SYSTEM,
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
) -> Prompt:
    """Propose signs from one bounded packet; document membership is assigned later."""
    return Prompt(
        system=(
            "The current library boundary failed review. Design a replacement boundary from "
            "this bounded evidence packet. This is one packet from a larger subtree, so do not "
            "return document IDs. Name one corpus-evidenced property and two or more reusable "
            "class signs on that property. Signs must be mutually exclusive, useful for ruling "
            "alternatives out, and written in the documents' own language. Notes are short "
            "positive routing rules, not inventories, counts, exclusions, or process narration. "
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
        + "\n".join(f"  - {sign.name}/ — {sign.note}" for sign in sketch.signs)
        for index, sketch in enumerate(sketches, start=1)
    )
    return Prompt(
        system=(
            "Consolidate candidate library boundaries produced from separate evidence packets. "
            "Return one boundary on one property whose signs can cover the classes evidenced "
            "across all candidates. Resolve synonyms and competing axes; do not mix axes or add "
            "a remainder class. Use the archive's own language. Notes are short positive routing "
            "rules. Return no document IDs and no explanation."
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
        f"  [G{index:03d}] {sign.name}/ — {sign.note}"
        for index, sign in enumerate(sketch.signs, start=1)
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
        "This replaces the whole boundary, so every document must be represented."
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
            "and distinctions that do not help a reader rule alternatives out. A note is a "
            "short positive routing sign for its own members, never an analysis of candidate "
            "axes or a description of excluded or leftover documents."
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


def _listing(
    path: str, purpose: str, documents: list[tuple[str, str]], children: list[tuple[str, str]]
) -> str:
    return _LISTING.format(
        path=path or "(루트)",
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
        path=path or "(루트)",
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
    return f"EXISTING SUB-FOLDERS:\n{rendered}"
