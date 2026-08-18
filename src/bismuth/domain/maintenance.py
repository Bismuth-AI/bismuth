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


def _is_phrase(label: str) -> bool:
    """Whether this name is several words rather than one.

    A one-word ancestor is refined by adding words -- 금융 → 금융소비자보호 -- so a child
    containing it says something new. A whole phrase is not refined that way; a child that
    still contains it has only decorated it, as 기업 유형별 지원 → 기업 유형별 지원 법령.
    Counting words rather than characters because the phrase that prompted this normalises
    to seven characters and is four words. Scripts that do not space their words read as
    one word and fall to the permissive side, which is the safe direction.
    """
    return len(label.split()) >= 2


def restates(inner: str, outer: str) -> bool:
    """Whether ``inner`` says nothing ``outer`` has not already said, above it.

    Both directions, for different reasons.

    A descendant whose whole name sits inside an ancestor's repeats a distinction already
    fixed to one value at that depth -- observed as ``대통령령 총리령(하위시행규정)/…/
    대통령령``, where the grandchild names one half of a compound its ancestor resolved.

    The other direction was left open as ordinary refinement, and the model used it to
    decorate: ``연구인프라 및 인력 지원`` gained the child ``국가연구인프라 및 인력 지원``,
    and ``기업 유형별 지원`` gained ``기업 유형별 지원 법령`` (two documents above
    thirty-eight, a pass-through in all but the count). Every document in the parent
    answers to such a name -- that is what made it the parent's name -- so it sorts
    nothing. Saying so in the prompt did not stop it recurring, which is what moved it
    here.

    Only when the ancestor's name is several words, though. A one-word ancestor is refined
    by adding words, not decorated by them, and refusing that would forbid most real trees.

    Compared on the normalised form, so punctuation and spacing cannot smuggle a repeat
    past an equality test -- which is exactly how the first observed case got through.
    """
    small, large = normalise_label(inner), normalise_label(outer)
    if not small or not large or small == large:
        return False
    if small in large:
        return True
    return large in small and _is_phrase(outer)


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
    NO_DIVISION = "single group took the folder, leaving no remainder to divide from"
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
    # Equality here. The model now holds the rest of this judgement, with the ancestors'
    # properties in front of it, which is what containment was standing in for and could
    # not do: it blocked 113 of 300 proposals in one round -- a root divided on 법령의
    # 주된 규제 대상 및 목적 leaves a child almost no words -- and simply dropping it
    # produced chains instead, each level restating its parent in other words. Whether
    # two properties are the same distinction is not a question about strings.
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

    # A drawn class that leaves one document behind is a rename with an outlier attached:
    # the reader pays an extra level that rules nothing out. Measured on 300 documents as
    # three folders holding a single document and a single child. SINGLE_DOCUMENT already
    # says a class of one document is not a class; the remainder deserves the same rule.
    if not allow_no_division and len(groups) == 1 and len(available_document_ids - assigned) < 2:
        problems.append(PlanProblem.NO_DIVISION)
    if require_complete and assigned != available_document_ids:
        problems.append(PlanProblem.UNASSIGNED_DOCUMENT)
    return PlanValidation(tuple(dict.fromkeys(problems)))


class GroupingProblem(StrEnum):
    """Why a proposal to stand existing folders on one broader shelf was refused."""

    INVALID_NAME = "the broader name is empty or unusable"
    NAME_TOO_LONG = "the broader name does not fit one path segment"
    NAME_IS_A_PATH = "the broader name contains a path separator"
    NAME_IS_A_SCHEMA_FIELD = "the broader name is a field of the answer schema"
    NAME_EXISTS = "a sub-folder of that name already stands here"
    NAME_IS_A_MEMBER = "the broader name is one of the folders it would contain"
    MEMBER_RESTATES_NAME = "a folder moving onto the shelf says the shelf's name again"
    NAME_RESTATES_AXIS = "the broader name repeats the question instead of answering it"
    ANCESTOR_NAME = "the broader name carries an ancestor's name"
    TOO_FEW_MEMBERS = "fewer than two folders would move onto the shelf"
    TOOK_EVERY_FOLDER = "every folder would move, which renames this folder rather than tidying it"
    UNKNOWN_MEMBER = "a folder named for the shelf does not stand here"


@dataclass(frozen=True, slots=True)
class GroupingValidation:
    problems: tuple[GroupingProblem, ...]

    @property
    def accepted(self) -> bool:
        return not self.problems


def validate_grouping(
    *,
    name: str,
    axis: str,
    members: tuple[str, ...],
    siblings: tuple[str, ...],
    ancestor_names: tuple[str, ...] = (),
) -> GroupingValidation:
    """Whether existing sub-folders may be stood together under one broader name.

    This operation moves folders, never documents: every document keeps the folder it is
    in and only the path above it changes. So the contracts here are all about the shape
    of the move -- that it groups more than one thing, that it leaves something behind to
    be grouped away from, and that the new name is usable as one path segment.
    """
    problems: list[GroupingProblem] = []
    cleaned = " ".join(name.split()).strip()
    key = normalise_label(cleaned)
    if not cleaned or not key:
        problems.append(GroupingProblem.INVALID_NAME)
    if len(cleaned) > MAX_SEGMENT:
        problems.append(GroupingProblem.NAME_TOO_LONG)
    if any(separator in cleaned.rstrip("/\\") for separator in ("/", "\\")):
        problems.append(GroupingProblem.NAME_IS_A_PATH)
    if key in SCHEMA_FIELD_NAMES:
        problems.append(GroupingProblem.NAME_IS_A_SCHEMA_FIELD)
    if key and key == normalise_label(axis):
        problems.append(GroupingProblem.NAME_RESTATES_AXIS)

    sibling_keys = {normalise_label(item) for item in siblings}
    member_keys = {normalise_label(item) for item in members}
    if key in member_keys:
        problems.append(GroupingProblem.NAME_IS_A_MEMBER)
    elif key in sibling_keys:
        problems.append(GroupingProblem.NAME_EXISTS)
    # The same contract a name is held to when it is created, applied again when a folder
    # is moved underneath one. It was only checked at creation, so a shelf could be built
    # over a folder that restates it: 위반 행위 및 제재 was stood over 위반 행위 및 제재
    # 유형, which restates() refuses outright between a parent and a child. That corridor
    # of near-synonyms reached six levels and left an empty folder in the middle of it.
    if any(restates(member, cleaned) for member in members):
        problems.append(GroupingProblem.MEMBER_RESTATES_NAME)
    if key in {normalise_label(item) for item in ancestor_names} or any(
        restates(cleaned, item) for item in ancestor_names
    ):
        problems.append(GroupingProblem.ANCESTOR_NAME)

    unique = tuple(dict.fromkeys(member_keys))
    if len(unique) < 2:
        problems.append(GroupingProblem.TOO_FEW_MEMBERS)
    if not member_keys <= sibling_keys:
        problems.append(GroupingProblem.UNKNOWN_MEMBER)
    # Something has to stay beside the new shelf. Moving every folder under one name
    # leaves this folder with a single child that rules nothing out -- the pass-through
    # SPEC.md 3.3.1 counts as a defect, arrived at from the other direction.
    elif len(unique) >= len(sibling_keys):
        problems.append(GroupingProblem.TOOK_EVERY_FOLDER)
    return GroupingValidation(tuple(dict.fromkeys(problems)))
