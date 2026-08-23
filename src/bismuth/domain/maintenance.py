"""Validation rules for proposed folder-structure changes."""

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
"""Schema identifiers that cannot be used as class names."""


def _is_phrase(label: str) -> bool:
    """Return whether a label contains multiple whitespace-delimited words."""
    return len(label.split()) >= 2


def restates(inner: str, outer: str) -> bool:
    """Return whether a child label merely repeats an ancestor label."""
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
    NAME_STANDS_ELSEWHERE = "a folder of that name already stands somewhere else"
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


def validate_names(
    *,
    axis: str,
    axis_question: str = "",
    names: tuple[str, ...],
    ancestor_names: tuple[str, ...] = (),
    spent_axes: tuple[str, ...] = (),
    taken_anywhere: frozenset[str] = frozenset(),
) -> PlanValidation:
    """Validate an axis and proposed names without document membership."""
    problems: list[PlanProblem] = []
    if not is_axis_label(axis):
        problems.append(PlanProblem.INVALID_AXIS)
    if not axis_question.strip() or "\n" in axis_question or "\r" in axis_question:
        problems.append(PlanProblem.MISSING_AXIS_QUESTION)
    wanted_axis = normalise_label(axis)
    if wanted_axis and any(wanted_axis == normalise_label(item) for item in spent_axes):
        problems.append(PlanProblem.SPENT_AXIS)

    seen: set[str] = set()
    ancestor_keys = {normalise_label(item) for item in ancestor_names}
    for proposed in names:
        name = " ".join(proposed.split()).strip()
        key = normalise_label(name)
        if not name or not key:
            problems.append(PlanProblem.INVALID_NAME)
        if len(name) > MAX_SEGMENT:
            problems.append(PlanProblem.NAME_TOO_LONG)
        if any(separator in name.rstrip("/\\") for separator in ("/", "\\")):
            problems.append(PlanProblem.NAME_IS_A_PATH)
        if key in SCHEMA_FIELD_NAMES:
            problems.append(PlanProblem.NAME_IS_A_SCHEMA_FIELD)
        if key and key == wanted_axis:
            problems.append(PlanProblem.AXIS_AS_NAME)
        if key in ancestor_keys or any(restates(name, item) for item in ancestor_names):
            problems.append(PlanProblem.ANCESTOR_NAME)
        if key in seen:
            problems.append(PlanProblem.DUPLICATE_NAME)
        if key in taken_anywhere:
            problems.append(PlanProblem.NAME_STANDS_ELSEWHERE)
        seen.add(key)
    return PlanValidation(tuple(dict.fromkeys(problems)))


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
    depth: int = 0,
) -> PlanValidation:
    """Validate a complete proposal before the first filesystem operation is built."""
    problems: list[PlanProblem] = []
    if not is_axis_label(axis):
        problems.append(PlanProblem.INVALID_AXIS)
    if not axis_question.strip() or "\n" in axis_question or "\r" in axis_question:
        problems.append(PlanProblem.MISSING_AXIS_QUESTION)
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
        # Class names must fit a single path segment without rewriting.
        if len(name) > MAX_SEGMENT:
            problems.append(PlanProblem.NAME_TOO_LONG)
        # A trailing separator is punctuation; an internal separator proposes a path.
        if any(separator in name.rstrip("/\\") for separator in ("/", "\\")):
            problems.append(PlanProblem.NAME_IS_A_PATH)
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
        elif len(members) < smallest_class(depth) and not allow_single_document:
            problems.append(PlanProblem.SINGLE_DOCUMENT)
        for document_id in members:
            if document_id not in available_document_ids:
                problems.append(PlanProblem.UNKNOWN_DOCUMENT)
            if document_id in assigned:
                problems.append(PlanProblem.DUPLICATE_DOCUMENT)
            assigned.add(document_id)

    # A division must leave enough documents outside the proposed class.
    if (
        not allow_no_division
        and len(groups) == 1
        and len(available_document_ids - assigned) < smallest_class(depth)
    ):
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
    NAME_STANDS_ELSEWHERE = "a folder of that name already stands somewhere else"
    SHELF_IS_NOT_HERE = "the folder they would move into does not stand here"
    NAME_IS_A_MEMBER = "the broader name is one of the folders it would contain"
    MEMBER_RESTATES_NAME = "a folder moving onto the shelf says the shelf's name again"
    NAME_RESTATES_AXIS = "the broader name repeats the question instead of answering it"
    ANCESTOR_NAME = "the broader name carries an ancestor's name"
    TOO_FEW_MEMBERS = "fewer than two folders would move onto the shelf"
    TOOK_EVERY_FOLDER = "every folder would move, which renames this folder rather than tidying it"
    UNKNOWN_MEMBER = "a folder named for the shelf does not stand here"
    SHELF_WOULD_GO_TOO_DEEP = "the shelf would push a folder past the depth a reader can follow"


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
    into_existing: bool = False,
    taken_anywhere: frozenset[str] = frozenset(),
    depth: int = 0,
    member_depths: tuple[int, ...] = (),
) -> GroupingValidation:
    """Validate grouping sibling folders under a new or existing parent."""
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
    if not into_existing and key in taken_anywhere:
        problems.append(GroupingProblem.NAME_STANDS_ELSEWHERE)
    if key in member_keys:
        problems.append(GroupingProblem.NAME_IS_A_MEMBER)
    elif into_existing and key not in sibling_keys:
        problems.append(GroupingProblem.SHELF_IS_NOT_HERE)
    elif key in sibling_keys and not into_existing:
        problems.append(GroupingProblem.NAME_EXISTS)
    # Moving a folder must not make it restate its new parent.
    if any(restates(member, cleaned) for member in members):
        problems.append(GroupingProblem.MEMBER_RESTATES_NAME)
    # Account for the deepest subtree moved under the new parent.
    if member_depths and depth + 1 + max(member_depths) > MAX_DEPTH:
        problems.append(GroupingProblem.SHELF_WOULD_GO_TOO_DEEP)
    if key in {normalise_label(item) for item in ancestor_names} or any(
        restates(cleaned, item) for item in ancestor_names
    ):
        problems.append(GroupingProblem.ANCESTOR_NAME)

    unique = tuple(dict.fromkeys(member_keys))
    if len(unique) < (1 if into_existing else 2):
        problems.append(GroupingProblem.TOO_FEW_MEMBERS)
    if not member_keys <= sibling_keys:
        problems.append(GroupingProblem.UNKNOWN_MEMBER)
    # At least one sibling must remain beside the grouped parent.
    elif len(unique) + (1 if into_existing else 0) >= len(sibling_keys):
        problems.append(GroupingProblem.TOOK_EVERY_FOLDER)
    return GroupingValidation(tuple(dict.fromkeys(problems)))


class SplitProblem(StrEnum):
    """Why a level may not be dissolved and its contents promoted."""

    IS_THE_ROOT = "the root has nowhere to promote to"
    NOTHING_TO_PROMOTE = "the level holds nothing that could stand one step up"
    NAME_TAKEN = "something of that name already stands where it would be promoted to"
    PROMOTED_RESTATES_ANCESTOR = "a promoted name would say what its new ancestor says"


@dataclass(frozen=True, slots=True)
class SplitValidation:
    problems: tuple[SplitProblem, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.problems


def validate_split(
    *,
    promoted: tuple[str, ...],
    ancestor_names: tuple[str, ...],
    taken: tuple[str, ...],
    documents: int = 0,
) -> SplitValidation:
    """Validate dissolving a folder and promoting its contents one level."""
    problems: list[SplitProblem] = []
    if not ancestor_names and not promoted and not documents:
        problems.append(SplitProblem.IS_THE_ROOT)
    if not promoted and not documents:
        problems.append(SplitProblem.NOTHING_TO_PROMOTE)

    taken_keys = {normalise_label(item) for item in taken}
    for name in promoted:
        if normalise_label(name) in taken_keys:
            problems.append(SplitProblem.NAME_TAKEN)
        if any(restates(name, item) for item in ancestor_names):
            problems.append(SplitProblem.PROMOTED_RESTATES_ANCESTOR)
    return SplitValidation(tuple(dict.fromkeys(problems)))


class Operator(StrEnum):
    """Structurally supported folder-maintenance operations."""

    KEEP = "keep"
    CREATE = "create"
    MERGE = "merge"
    SPLIT = "split"


REVERSE_OF = {Operator.MERGE: Operator.SPLIT, Operator.SPLIT: Operator.MERGE}
"""Operations that reverse each other."""

MIN_CLASS_DOCUMENTS = 2
"""A class of one document is a rename, not a class (``SINGLE_DOCUMENT``)."""

MIN_REMAINDER_DOCUMENTS = 2
"""What has to be left behind for a class to have divided anything (``NO_DIVISION``)."""


def smallest_class(depth: int) -> int:
    """Return the minimum useful class size at a tree depth."""
    return max(MIN_CLASS_DOCUMENTS, depth + 1)


MIN_GROUPING_MEMBERS = 2
"""Fewer than two folders on a new shelf is a rename (``TOO_FEW_MEMBERS``)."""

MAX_DEPTH = 4
"""Maximum folder levels between the vault root and a document."""


@dataclass(frozen=True, slots=True)
class FolderShape:
    """Structural facts used to select legal maintenance operations."""

    loose_documents: int
    depth: int = 0
    """How many levels below the root this folder sits. The bar for a new class rises
    with it, because a level here costs a guess behind every guess above."""
    children: tuple[str, ...] = ()
    ancestor_names: tuple[str, ...] = ()
    siblings: tuple[str, ...] = ()
    is_root: bool = False
    subtree_depth: int = 0
    """How many levels of folder stand below this one; ``0`` for a leaf. Counted from the
    filesystem, because grouping pushes a whole subtree down by one and the ceiling is
    about where the deepest document ends up, not about where the shelf goes."""
    last_operator: Operator | None = None
    evidence_moved: bool = True
    """Whether this folder's own evidence has moved since ``last_operator`` was applied."""


def legal_operators(shape: FolderShape) -> frozenset[Operator]:
    """Return operations that can produce a structurally valid result."""
    legal = {Operator.KEEP}

    # A new class and its remainder must both meet the depth-adjusted size floor.
    if shape.loose_documents >= smallest_class(shape.depth) * 2 and shape.depth < MAX_DEPTH:
        legal.add(Operator.CREATE)

    # Grouping requires multiple members, a remaining sibling, and available depth.
    if len(shape.children) > MIN_GROUPING_MEMBERS and shape.depth + 1 < MAX_DEPTH:
        legal.add(Operator.MERGE)

    if (
        not shape.is_root
        and validate_split(
            promoted=shape.children,
            ancestor_names=shape.ancestor_names,
            taken=shape.siblings,
            documents=shape.loose_documents,
        ).accepted
    ):
        legal.add(Operator.SPLIT)

    # Do not immediately reverse the last operation without new evidence.
    if not shape.evidence_moved and shape.last_operator in REVERSE_OF:
        legal.discard(REVERSE_OF[shape.last_operator])

    return frozenset(legal)
