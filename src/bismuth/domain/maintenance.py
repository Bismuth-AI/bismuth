"""Pure rules for changes proposed by the library-maintenance model.

The model proposes a classification change; it never commands the filesystem.  These
rules are the border between an opinion and an operation.  Keeping them in the domain
means every adapter and every model is held to the same invariants.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from bismuth.domain.paths import MAX_SEGMENT


def normalise_label(value: str) -> str:
    """A comparison form for model-authored labels."""
    return "".join(character for character in value.casefold() if character.isalnum())


SCHEMA_FIELD_NAMES = frozenset(
    {
        "axis",
        "axisquestion",
        "sign",
        "name",
        "emerged",
        "basis",
        "basisquestion",
        "signs",
        "groups",
        "note",
        "documentids",
    }
)
"""Normalised names of the fields Bismuth's own schemas expose. Not a vocabulary about
documents -- these are our identifiers, and a class named after one is a decoding
artefact rather than an answer."""


def restates(inner: str, outer: str) -> bool:
    """Whether ``inner`` says nothing ``outer`` has not already said, above it.

    One direction only, and deliberately.  A descendant whose whole name sits inside an
    ancestor's is repeating a distinction that is already fixed to one value at that
    depth -- observed as ``대통령령 총리령(하위시행규정)/…/대통령령``, where the
    grandchild names one half of a compound its ancestor had already resolved.  The
    other direction is ordinary refinement: a child adding words to an ancestor's name
    is usually saying something new, and rejecting it would forbid most real trees.

    Compared on the normalised form, so punctuation and spacing cannot smuggle a repeat
    past an equality test -- which is exactly how the observed case got through.
    """
    small, large = normalise_label(inner), normalise_label(outer)
    return bool(small) and bool(large) and small != large and small in large


def is_axis_label(value: str) -> bool:
    """Whether ``value`` is structurally safe as a single-line axis label.

    Meaning is deliberately not guessed here.  Word counts, punctuation lists and
    English opening-word blacklists made this boundary language-specific while still
    accepting bad labels in other languages.  The model judges meaning; the domain
    only rejects empty, multiline and control-character state.
    """
    text = " ".join(value.split()).strip()
    return bool(text) and all(character.isprintable() for character in value) and "\n" not in value


class PlanProblem(StrEnum):
    """Why a model-authored maintenance plan is unsafe or meaningless."""

    INVALID_AXIS = "invalid axis"
    MISSING_AXIS_QUESTION = "axis has no question"
    SPENT_AXIS = "axis already used above here"
    INVALID_NAME = "invalid class name"
    NAME_TOO_LONG = "class name is longer than a routing sign"
    NAME_IS_A_PATH = "class name contains a path separator"
    NAME_IS_A_SCHEMA_FIELD = "class name is a field of the schema, not an answer"
    AXIS_AS_NAME = "class name repeats the axis instead of answering it"
    ANCESTOR_NAME = "class carries an ancestor's name"
    DUPLICATE_NAME = "duplicate class name"
    UNKNOWN_DOCUMENT = "unknown document"
    DUPLICATE_DOCUMENT = "document assigned to more than one class"
    SINGLE_DOCUMENT = "a sign points at one document"
    NO_MEMBERS = "class has no documents"
    NO_DIVISION = "single group took every document"
    UNASSIGNED_DOCUMENT = "review leaves documents outside the new boundary"


@dataclass(frozen=True, slots=True)
class ProposedClass:
    """One shelf proposed by the model, before paths or operations exist."""

    name: str
    document_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanValidation:
    """The complete result so callers can trace every problem, not only the first."""

    problems: tuple[PlanProblem, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.problems


def validate_plan(
    *,
    axis: str,
    axis_question: str = "",
    groups: tuple[ProposedClass, ...],
    available_document_ids: frozenset[str],
    ancestor_names: tuple[str, ...] = (),
    spent_axes: tuple[str, ...] = (),
    require_complete: bool = False,
    allow_single_document: bool = False,
    allow_no_division: bool = False,
) -> PlanValidation:
    """Validate a complete proposal before the first filesystem operation is built."""
    problems: list[PlanProblem] = []
    if not is_axis_label(axis):
        problems.append(PlanProblem.INVALID_AXIS)
    if not axis_question.strip() or "\n" in axis_question or "\r" in axis_question:
        problems.append(PlanProblem.MISSING_AXIS_QUESTION)
    # Equality, not containment. Containment was added here before the semantic layer had
    # a check of its own, and it starved the tree the moment an ancestor's property was a
    # compound phrase: with a root divided on 법령의 규제 대상 및 산업 분야, every narrower
    # property a child could name shared words with it, and 73 of 120 proposals in one
    # round died on this line. A child sharing vocabulary with its ancestor is not the
    # same as a child repeating the distinction, and only meaning can tell them apart --
    # which is what the axis check does.
    wanted_axis = normalise_label(axis)
    if wanted_axis and any(wanted_axis == normalise_label(item) for item in spent_axes):
        problems.append(PlanProblem.SPENT_AXIS)

    names: set[str] = set()
    assigned: set[str] = set()
    ancestor_keys = {normalise_label(item) for item in ancestor_names}
    for group in groups:
        name = " ".join(group.name.split()).strip()
        key = normalise_label(name)
        if not name or not key:
            problems.append(PlanProblem.INVALID_NAME)
        # A name is going to become one path segment. One that has to be cut to fit was
        # never a sign; the cut just hid that a sentence had been proposed.
        if len(name) > MAX_SEGMENT:
            problems.append(PlanProblem.NAME_TOO_LONG)
        # A separator inside the name means a path was proposed, and sanitisation would
        # quietly flatten it so the folder landed a level up from where the model said.
        # A trailing one is punctuation -- "과학기술정보통신부/" names that folder and
        # nothing else -- and refusing it cost nine subdivisions in one 120-document
        # round. Strip the tail, refuse the rest.
        if any(separator in name.rstrip("/\\") for separator in ("/", "\\")):
            problems.append(PlanProblem.NAME_IS_A_PATH)
        # Constrained decoding fills fields in order, and a small model can fill one with
        # the name of the next. Observed: a class literally named "emerged", which reached
        # validation and was refused for an unrelated reason.
        if key in SCHEMA_FIELD_NAMES:
            problems.append(PlanProblem.NAME_IS_A_SCHEMA_FIELD)
        if key and key == wanted_axis:
            problems.append(PlanProblem.AXIS_AS_NAME)
        if key in ancestor_keys or any(restates(name, item) for item in ancestor_names):
            problems.append(PlanProblem.ANCESTOR_NAME)
        if key in names:
            problems.append(PlanProblem.DUPLICATE_NAME)
        names.add(key)

        members = tuple(dict.fromkeys(group.document_ids))
        if len(members) != len(group.document_ids):
            problems.append(PlanProblem.DUPLICATE_DOCUMENT)
        if not members:
            problems.append(PlanProblem.NO_MEMBERS)
        elif len(members) == 1 and not allow_single_document:
            problems.append(PlanProblem.SINGLE_DOCUMENT)
        for document_id in members:
            if document_id not in available_document_ids:
                problems.append(PlanProblem.UNKNOWN_DOCUMENT)
            if document_id in assigned:
                problems.append(PlanProblem.DUPLICATE_DOCUMENT)
            assigned.add(document_id)

    if not allow_no_division and len(groups) == 1 and assigned == available_document_ids:
        problems.append(PlanProblem.NO_DIVISION)
    if require_complete and assigned != available_document_ids:
        problems.append(PlanProblem.UNASSIGNED_DOCUMENT)
    return PlanValidation(tuple(dict.fromkeys(problems)))
