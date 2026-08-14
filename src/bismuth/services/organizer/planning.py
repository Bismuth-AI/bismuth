"""Typed shadow plans and deterministic whole-vault validation."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from agentkit import FunctionTool, Tool
from pydantic import BaseModel

from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.document import sidecar_name
from bismuth.domain.paths import sanitize_segment
from bismuth.logging_setup import log_trace
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.organizer.prompts import PlanOperation
from bismuth.services.organizer.tools import (
    _BoundaryPlan,
    _SubmitIncrementalPlanArgs,
    _SubmitInitialPlanArgs,
    _SubmitPlanArgs,
)
from bismuth.services.sidecar import read_sidecar_meta


@dataclass(frozen=True, slots=True)
class ProposedMove:
    """One validated move in a complete shadow plan."""

    paths: list[str]
    target: str


@dataclass(frozen=True, slots=True)
class ProposedRename:
    """Legacy manual-organizer shape; autonomous plans do not rename folders."""

    folder: str
    new_name: str


@dataclass(frozen=True, slots=True)
class ProposedBoundary:
    """One parent boundary and all sibling moves proposed beneath it."""

    parent: str
    operation: PlanOperation
    axis: str
    axis_question: str
    moves: list[ProposedMove]


@dataclass(frozen=True, slots=True)
class ReorgProposal:
    """A validated, still-unapplied shadow plan and the agent's explanation."""

    moves: list[ProposedMove]
    renames: list[ProposedRename]
    summary: str
    boundaries: list[ProposedBoundary]
    problems: list[str]


@dataclass(frozen=True, slots=True)
class ReorgResult:
    """Outcome of an autonomous plan/validate/apply cycle."""

    proposal: ReorgProposal
    applied: bool
    moved: int = 0
    unresolved_document_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ReviewIssue:
    """One blocking semantic finding and the candidate state that caused it."""

    problem: str
    kind: str
    evidence_handles: tuple[str, ...]
    candidate_signature: str


@dataclass(frozen=True, slots=True)
class _ReviewOutcome:
    problems: tuple[str, ...] = ()
    issues: tuple[_ReviewIssue, ...] = ()


def _candidate_payload(boundaries: Sequence[ProposedBoundary]) -> list[dict[str, object]]:
    return [
        {
            "parent": boundary.parent,
            "operation": boundary.operation,
            "axis": boundary.axis,
            "axis_question": boundary.axis_question,
            "moves": [
                {"target": move.target, "paths": sorted(move.paths)}
                for move in sorted(boundary.moves, key=lambda item: item.target)
            ],
        }
        for boundary in sorted(boundaries, key=lambda item: item.parent)
    ]


def _candidate_fingerprint(boundaries: Sequence[ProposedBoundary]) -> str:
    raw = json.dumps(_candidate_payload(boundaries), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _finding_signature(
    boundaries: Sequence[ProposedBoundary],
    evidence_handles: Sequence[str],
    *,
    handles: dict[str, PurePosixPath],
    kind: str = "",
) -> str:
    """Describe only the placements implicated by a finding, or the whole boundary."""

    destinations = {path: path for path in handles.values()}
    for boundary in boundaries:
        for move in boundary.moves:
            target = PurePosixPath(move.target)
            for raw_path in move.paths:
                source = PurePosixPath(raw_path)
                destinations[source] = target / source.name
    cited_destinations = [
        (document_id, str(destinations[path].parent))
        for document_id in dict.fromkeys(evidence_handles)
        if (path := handles.get(document_id)) is not None
    ]
    # Relationship findings survive cosmetic shelf renames. Placement findings instead
    # track concrete destinations: moving cited documents to a corrected target must
    # clear the old finding rather than looking like a stale co-membership violation.
    cited: list[tuple[str, object]]
    relationship_kinds = {"overlap", "contains_sibling", "duplicate_boundary", "family_split"}
    if len(cited_destinations) > 1 and kind in relationship_kinds:
        labels: dict[str, int] = {}
        cited = []
        for document_id, destination in cited_destinations:
            label = labels.setdefault(destination, len(labels))
            cited.append((document_id, label))
    else:
        cited = []
        cited.extend(cited_destinations)
    payload: object = cited if cited else _candidate_payload(boundaries)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _plan_summary(
    boundaries: Sequence[ProposedBoundary],
    *,
    moved: int | None = None,
    unresolved_document_ids: Sequence[str] = (),
) -> str:
    """Render applied facts from the validated object, never from model narration."""

    prefix = "검증된 구조 계획"
    if moved is not None:
        prefix = f"구조 계획 적용 완료: 문서 {moved}개 이동"
    details = []
    for boundary in boundaries:
        parent = boundary.parent or "/"
        targets = ", ".join(
            f"{PurePosixPath(move.target).name} {len(move.paths)}개" for move in boundary.moves
        )
        details.append(f"{parent} [{boundary.operation}] — {targets}")
    parts = [prefix, *details]
    if unresolved_document_ids:
        parts.append(f"현재 scope에 남은 도착 문서 {len(unresolved_document_ids)}개")
    return "\n".join(parts)


def _folder(raw: str) -> PurePosixPath:
    if raw in ("", "/", "."):
        return PurePosixPath()
    return PurePosixPath(raw[1:] if raw.startswith("/") else raw)


def _within(path: PurePosixPath, parent: PurePosixPath) -> bool:
    return path.parts[: len(parent.parts)] == parent.parts


def _strict_folder(raw: str) -> PurePosixPath | None:
    """Accept a model path only when sanitising would leave every segment unchanged."""
    if "\\" in raw or raw.startswith("/") or raw.endswith("/"):
        return None
    parts = raw.split("/") if raw else []
    if not parts or any(not part or part in (".", "..") or part != part.strip() for part in parts):
        return None
    try:
        if any(sanitize_segment(part) != part for part in parts):
            return None
    except ValueError:
        return None
    return PurePosixPath(*parts)


def _boundary_parent(raw: str) -> PurePosixPath | None:
    """Parse a boundary parent while accepting the three common root spellings."""
    if raw in ("", "/", "."):
        return PurePosixPath()
    return _strict_folder(raw)


def _source_meta(vault: Vault, document: PurePosixPath) -> dict[str, object]:
    """Read only durable card metadata colocated with a document."""

    sidecar = document.parent / sidecar_name(document.name)
    if not vault.exists(sidecar):
        return {}
    return read_sidecar_meta(vault.read_text(sidecar)) or {}


def _normalised_family_text(value: object) -> str:
    """Comparable letters/digits without assuming a language or legal vocabulary."""

    return "".join(character.casefold() for character in str(value) if character.isalnum())


def _family_title(vault: Vault, document: PurePosixPath) -> str:
    """Return a title only when the source filename independently grounds it.

    Card models can occasionally emit the same generic title for unrelated fixtures or
    documents. Requiring the source stem to begin with the title keeps family locking tied
    to two independent pieces of durable evidence while remaining corpus-neutral.
    """

    meta = _source_meta(vault, document)
    title = _normalised_family_text(meta.get("title", ""))
    source = _normalised_family_text(meta.get("source", document.name))
    return title if len(title) >= 4 and source.startswith(title) else ""


def _same_document_family(left: str, right: str) -> bool:
    """Whether two grounded titles denote editions or subordinate instruments."""

    if not left or not right:
        return False
    shorter, longer = sorted((left, right), key=len)
    return shorter == longer or (
        len(shorter) >= 6 and longer.startswith(shorter) and len(shorter) / len(longer) >= 0.6
    )


def _family_groups(vault: Vault, documents: Collection[PurePosixPath]) -> list[set[PurePosixPath]]:
    """Build small connected components of title-grounded document families."""

    paths = sorted(documents, key=str)
    titles = {path: _family_title(vault, path) for path in paths}
    parent = {path: path for path in paths}

    def find(path: PurePosixPath) -> PurePosixPath:
        while parent[path] != path:
            parent[path] = parent[parent[path]]
            path = parent[path]
        return path

    def union(left: PurePosixPath, right: PurePosixPath) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if _same_document_family(titles[left], titles[right]):
                union(left, right)
    groups: dict[PurePosixPath, set[PurePosixPath]] = {}
    for path in paths:
        groups.setdefault(find(path), set()).add(path)
    return [group for group in groups.values() if len(group) > 1]


def _script_name(character: str) -> str:
    """A Unicode-derived writing-system token, not a configured language list."""

    if not character.isalpha():
        return ""
    name = unicodedata.name(character, "")
    if not name:
        return ""
    return name.split(" ", 1)[0]


def _dominant_document_script(vault: Vault, documents: Collection[PurePosixPath]) -> str:
    """Infer the collection's writing system from its own durable titles."""

    counts: dict[str, int] = {}
    total = 0
    for document in documents:
        title = str(_source_meta(vault, document).get("title", ""))
        for character in title:
            script = _script_name(character)
            if not script:
                continue
            counts[script] = counts.get(script, 0) + 1
            total += 1
    if not counts or total == 0:
        return ""
    script, count = max(counts.items(), key=lambda item: item[1])
    return script if count / total >= 0.6 else ""


def _uses_script(value: str, script: str) -> bool:
    return not script or any(_script_name(character) == script for character in value)


def _coalesce_boundary_plans(
    boundaries: Sequence[_BoundaryPlan],
) -> tuple[list[_BoundaryPlan], list[str]]:
    """Combine the one safe same-parent composite operation.

    Extending a boundary and routing other loose arrivals are one atomic change to the
    same sibling set. Qwen naturally emitted them as two objects; rejecting that shape
    left valid documents loose. Other operation mixtures retain distinct preconditions
    and remain invalid rather than being guessed into a stronger operation.
    """

    grouped: dict[str, list[_BoundaryPlan]] = {}
    order: list[str] = []
    for boundary in boundaries:
        parsed = _boundary_parent(boundary.parent)
        key = _stored_folder(parsed) if parsed is not None else f"invalid:{boundary.parent}"
        if key not in grouped:
            order.append(key)
        grouped.setdefault(key, []).append(boundary)

    result: list[_BoundaryPlan] = []
    problems: list[str] = []
    for key in order:
        group = grouped[key]
        if len(group) == 1:
            result.append(group[0])
            continue
        operations = {boundary.operation for boundary in group}
        if operations <= {"route_existing", "add_sibling"} and "add_sibling" in operations:
            extending = next(boundary for boundary in group if boundary.operation == "add_sibling")
            result.append(
                _BoundaryPlan(
                    parent=extending.parent,
                    operation="add_sibling",
                    axis=extending.axis,
                    axis_question=extending.axis_question,
                    moves=[move for boundary in group for move in boundary.moves],
                )
            )
            continue
        problems.append(
            f"{key or '/'} is submitted more than once with incompatible operations: "
            + ", ".join(sorted(operations))
        )
    return result, problems


def _stored_folder(path: PurePosixPath) -> str:
    """Keep the vault root stable across validation and apply-time revalidation."""
    return "" if not path.parts else str(path)


def _direct_child_target(parent: PurePosixPath, raw: str) -> PurePosixPath | None:
    """Resolve a class label relative to its boundary, retaining old full paths."""

    parsed = _strict_folder(raw)
    if parsed is None:
        return None
    if len(parsed.parts) == 1:
        return parent / parsed
    if parsed.parent == parent:
        return parsed
    return None


def _validate_shadow_plan(
    vault: Vault,
    args: _SubmitPlanArgs,
    *,
    scope: PurePosixPath,
    handles: dict[str, PurePosixPath],
    family_units: dict[str, tuple[str, ...]] | None = None,
    require_family_units: bool = False,
) -> tuple[list[ProposedBoundary], list[str]]:
    """Validate and simulate a complete plan without changing the vault."""
    submitted_boundaries, problems = _coalesce_boundary_plans(args.boundaries)
    units = family_units or {}
    unit_by_member = {member: unit for unit, members in units.items() for member in members}
    accepted: list[ProposedBoundary] = []
    assigned: set[PurePosixPath] = set()
    planned_destinations: set[str] = set()

    if not args.boundaries:
        return [], ["the submitted plan has no boundaries"]

    for boundary in submitted_boundaries:
        parent = _boundary_parent(boundary.parent)
        if parent is None:
            problems.append(f"invalid boundary parent: {boundary.parent!r}")
            continue
        if not vault.is_dir(parent):
            problems.append(f"boundary parent does not exist: {parent or '/'}")
            continue
        if parent != scope:
            problems.append(
                f"boundary parent must equal the assigned scope {scope or '/'}: {parent or '/'}"
            )
            continue
        available = set(vault.iter_files(parent, recursive=True))
        document_script = _dominant_document_script(vault, available)
        document_stems = {path.stem.casefold() for path in available}
        document_suffixes = {path.suffix.casefold() for path in available if path.suffix}
        all_folders = set(vault.iter_folders())
        direct_children = {
            folder
            for folder in all_folders
            if len(folder.parts) == len(parent.parts) + 1
            and folder.parts[: len(parent.parts)] == parent.parts
            and folder.parts[0] != INBOX.parts[0]
        }
        parent_charter_path = parent / CHARTER_FILENAME
        parent_charter = None
        if vault.exists(parent_charter_path):
            try:
                parent_charter = Charter.from_markdown(
                    vault.read_text(parent_charter_path), path=parent
                )
            except Exception:
                parent_charter = None
        if parent_charter is not None and not parent_charter.managed:
            problems.append(
                f"boundary parent is human-managed and cannot be changed: {parent or '/'}"
            )
            continue

        def child_is_managed(child: PurePosixPath) -> bool:
            note_path = child / CHARTER_FILENAME
            if not vault.exists(note_path):
                return True
            try:
                return Charter.from_markdown(vault.read_text(note_path), path=child).managed
            except Exception:
                return False

        existing_axis = parent_charter.split_basis if parent_charter is not None else ""
        existing_question = parent_charter.split_question if parent_charter is not None else ""
        axis = " ".join(boundary.axis.split()).strip()
        question = " ".join(boundary.axis_question.split()).strip()
        operation = boundary.operation

        if operation in {"route_existing", "rehome_existing"}:
            axis = existing_axis
            question = existing_question
            if not direct_children:
                problems.append(f"{parent or '/'} has no existing child boundary to route into")
        elif operation == "add_sibling":
            if not direct_children or not existing_axis or not existing_question:
                problems.append(f"{parent or '/'} has no established boundary to extend")
            # Incremental plans do not own the boundary contract. Ignore any legacy
            # axis fields and inherit the durable charter rather than turning harmless
            # model repetition into an impossible repair loop.
            axis = existing_axis
            question = existing_question
        elif operation == "create_boundary" and direct_children:
            problems.append(
                f"{parent or '/'} already has children; use add_sibling or replace_boundary"
            )
        elif operation == "replace_boundary" and not direct_children:
            problems.append(f"{parent or '/'} has no established boundary to replace")
        if operation == "replace_boundary":
            human_children = [child for child in direct_children if not child_is_managed(child)]
            if human_children:
                problems.append(
                    f"replace_boundary cannot change human-managed children: "
                    f"{', '.join(str(child) for child in sorted(human_children, key=str))}"
                )

        if operation in {"create_boundary", "replace_boundary"}:
            if not axis or "\n" in boundary.axis or "\r" in boundary.axis:
                problems.append(f"{parent or '/'} has an invalid axis")
            if not question or "\n" in boundary.axis_question or "\r" in boundary.axis_question:
                problems.append(f"{parent or '/'} has an invalid axis question")
            if document_script and not _uses_script(axis, document_script):
                problems.append(
                    f"{parent or '/'} axis does not use the documents' dominant writing system"
                )
            if document_script and not _uses_script(question, document_script):
                problems.append(
                    f"{parent or '/'} axis question does not use the documents' dominant "
                    "writing system"
                )

        targets: dict[PurePosixPath, list[PurePosixPath]] = {}
        for move in boundary.moves:
            target = _direct_child_target(parent, move.target)
            if target is None:
                problems.append(
                    f"target is not a safe direct child of {parent or '/'}: {move.target!r}"
                )
                continue
            if target.parts and target.parts[0] == INBOX.parts[0]:
                problems.append(f"target is inside the inbox: {target}")
                continue
            if vault.exists(target) and not vault.is_dir(target):
                problems.append(f"target is an existing file: {target}")
                continue
            if operation in {"route_existing", "rehome_existing"} and target not in direct_children:
                problems.append(f"{operation} target is not an existing direct child: {target}")
            if (
                operation in {"route_existing", "rehome_existing"}
                and target in direct_children
                and not child_is_managed(target)
            ):
                problems.append(f"{operation} target is human-managed: {target}")
            if operation == "add_sibling" and target in direct_children:
                # One atomic add_sibling plan may also route other loose arrivals into
                # established values. At least one genuinely new value is checked below.
                pass
            if (
                document_script
                and target not in direct_children
                and not _uses_script(target.name, document_script)
            ):
                problems.append(
                    f"new class {target.name!r} does not use the documents' dominant writing system"
                )
            if target.name.casefold() == axis.casefold():
                problems.append(f"class name repeats its axis: {target.name}")
            if re.search(r"(?i)(?:\bvs\.?\b|\bversus\b|↔)", target.name):
                problems.append(f"class name is a comparison, not one axis value: {target.name}")
            if target.name.casefold() in document_stems or any(
                target.name.casefold().endswith(suffix) for suffix in document_suffixes
            ):
                problems.append(f"class name copies a document filename: {target.name}")
            bucket = targets.setdefault(target, [])
            expanded_ids: list[str] = []
            for assignment_id in move.document_ids:
                if assignment_id in units:
                    expanded_ids.extend(units[assignment_id])
                    continue
                if require_family_units and assignment_id in unit_by_member:
                    problems.append(
                        f"family member {assignment_id} must be assigned with indivisible "
                        f"unit {unit_by_member[assignment_id]}"
                    )
                    continue
                expanded_ids.append(assignment_id)
            for document_id in dict.fromkeys(expanded_ids):
                source = handles.get(document_id)
                if source is None:
                    problems.append(f"unknown document handle: {document_id}")
                    continue
                if source in assigned:
                    problems.append(f"document is assigned more than once: {source}")
                    continue
                if source not in available:
                    problems.append(f"unknown document: {source}")
                    continue
                if not _within(source, parent) or not _within(source, scope):
                    problems.append(f"document is outside its boundary: {source}")
                    continue
                source_child = next(
                    (child for child in direct_children if _within(source, child)),
                    None,
                )
                if source_child == target:
                    # An F unit may include a committed family mate that already anchors
                    # the intended shelf. It proves the destination but needs no MOVE.
                    assigned.add(source)
                    continue
                if operation == "route_existing" and source.parent != parent:
                    problems.append(
                        f"route_existing may move only loose documents directly at {parent or '/'}: "
                        f"{source}"
                    )
                    continue
                if operation == "add_sibling" and source.parent != parent:
                    if source_child is None:
                        problems.append(
                            f"add_sibling may move only loose documents or documents below "
                            f"an existing direct child of {parent or '/'}: {source}"
                        )
                        continue
                    if not child_is_managed(source_child):
                        problems.append(f"add_sibling source is human-managed: {source_child}")
                        continue
                    source_ancestors = [
                        folder
                        for folder in all_folders
                        if folder != parent and _within(folder, parent) and _within(source, folder)
                    ]
                    human_source = next(
                        (folder for folder in source_ancestors if not child_is_managed(folder)),
                        None,
                    )
                    if human_source is not None:
                        problems.append(f"add_sibling source is human-managed: {human_source}")
                        continue
                if operation == "rehome_existing":
                    if source.parent != parent and source_child is None:
                        problems.append(
                            f"rehome_existing may move only loose documents or documents below "
                            f"a direct child of {parent or '/'}: {source}"
                        )
                        continue
                    if source.parent != parent:
                        source_ancestors = [
                            folder
                            for folder in all_folders
                            if folder != parent
                            and _within(folder, parent)
                            and _within(source, folder)
                        ]
                        human_source = next(
                            (folder for folder in source_ancestors if not child_is_managed(folder)),
                            None,
                        )
                        if human_source is not None:
                            problems.append(
                                f"rehome_existing source is human-managed: {human_source}"
                            )
                            continue
                destination = target / source.name
                destination_key = str(destination).casefold()
                if destination_key in planned_destinations:
                    problems.append(f"two documents would collide at {destination}")
                    continue
                if destination != source and vault.exists(destination):
                    problems.append(f"destination already contains {source.name}: {target}")
                    continue
                assigned.add(source)
                planned_destinations.add(destination_key)
                bucket.append(source)

        # Family membership is deterministic evidence, but validation must never rewrite
        # a model submission. Silent retargeting made the critic inspect a different plan
        # from the one the planner submitted and made correct revisions look stale.
        families = _family_groups(vault, available)
        handle_by_path = {path: handle for handle, path in handles.items()}

        def family_details(
            family: Collection[PurePosixPath],
            *,
            proposed: dict[PurePosixPath, PurePosixPath] | None = None,
            current_handles: dict[PurePosixPath, str] = handle_by_path,
        ) -> str:
            details: list[str] = []
            for document in sorted(family, key=str):
                handle = current_handles.get(document)
                if handle is None:
                    continue
                final = (proposed or {}).get(document)
                final_parent = final if final is not None else document.parent
                details.append(
                    f"{handle}(current={document.parent or PurePosixPath('.')}, "
                    f"final={final_parent or PurePosixPath('.')})"
                )
            return ", ".join(details)

        for family in families:
            assigned_family = {
                document: target
                for target, paths in targets.items()
                for document in paths
                if document in family
            }
            if not assigned_family:
                continue
            distinct_targets = set(assigned_family.values())
            if len(distinct_targets) > 1:
                names = sorted(
                    str(_source_meta(vault, document).get("title", document.stem))
                    for document in assigned_family
                )
                destinations = ", ".join(
                    str(target) for target in sorted(distinct_targets, key=str)
                )
                problems.append(
                    "document family is assigned to different targets "
                    f"({destinations}): "
                    + " | ".join(dict.fromkeys(names))
                    + f"; movable members: {family_details(family, proposed=assigned_family)}"
                )

        nonempty = {target: paths for target, paths in targets.items() if paths}
        # Normalization may erase every submitted move when all documents already sit
        # at the requested destination. That is no change, not an empty boundary.
        if not nonempty:
            continue
        if operation in {"create_boundary", "replace_boundary"} and len(nonempty) < 2:
            problems.append(
                f"{parent or '/'} cannot create a new boundary with fewer than two sibling classes"
            )
        if operation == "add_sibling" and not any(
            target not in direct_children for target in nonempty
        ):
            problems.append(f"{parent or '/'} add_sibling does not add a new sibling")
        for target, paths in nonempty.items():
            if not vault.exists(target) and len(paths) < 2:
                problems.append(f"new shelf {target} would contain only one document")
        proposed_target = {source: target for target, paths in nonempty.items() for source in paths}

        def final_direct_shelf(
            document: PurePosixPath,
            *,
            proposed: dict[PurePosixPath, PurePosixPath] = proposed_target,
            children: set[PurePosixPath] = direct_children,
            boundary_parent: PurePosixPath = parent,
        ) -> PurePosixPath:
            destination = proposed.get(document)
            if destination is not None:
                return destination
            return next(
                (child for child in children if _within(document, child)),
                boundary_parent,
            )

        for family in families:
            if not any(document in proposed_target for document in family):
                continue
            shelves = {final_direct_shelf(document) for document in family}
            if len(shelves) > 1:
                names = sorted(
                    str(_source_meta(vault, document).get("title", document.stem))
                    for document in family
                )
                problems.append(
                    "document family would be split across direct shelves: "
                    + " | ".join(dict.fromkeys(names))
                    + f"; movable members: {family_details(family, proposed=proposed_target)}"
                )
        if operation == "replace_boundary":
            old_child_documents = {
                path for path in available if any(_within(path, child) for child in direct_children)
            }
            missing = sorted(old_child_documents - assigned, key=str)
            if missing:
                problems.append(
                    f"{parent or '/'} replace_boundary omits {len(missing)} documents from old children"
                )
        if operation in {"add_sibling", "rehome_existing"}:
            # This least-powerful repair may not silently erase a boundary value. If all
            # documents below a source shelf leave, replacing the boundary is the honest
            # operation because the sibling set itself changes.
            final_documents = available - assigned
            final_documents.update(
                target / source.name for target, paths in nonempty.items() for source in paths
            )
            # Only direct children are values of this boundary. A misrouted family may
            # live in a deeper managed shelf; emptying and retiring that descendant is
            # cleanup, not removal of a sibling value at the reviewed parent.
            source_folders = [
                child
                for child in direct_children
                if any(_within(source, child) for source in assigned)
            ]
            emptied = [
                folder
                for folder in source_folders
                if not any(_within(document, folder) for document in final_documents)
            ]
            if emptied:
                problems.append(
                    f"{operation} would empty existing boundary values: "
                    f"{', '.join(str(child) for child in sorted(emptied, key=str))}"
                )
        accepted.append(
            ProposedBoundary(
                parent=_stored_folder(parent),
                operation=operation,
                axis=axis,
                axis_question=question,
                moves=[
                    ProposedMove(paths=[str(path) for path in paths], target=str(target))
                    for target, paths in nonempty.items()
                ],
            )
        )

    return accepted, list(dict.fromkeys(problems))


def build_submit_plan_tool(
    vault: Vault,
    *,
    scope: PurePosixPath,
    handles: dict[str, PurePosixPath],
    sink: list[ProposedBoundary],
    problem_sink: list[str],
    family_units: dict[str, tuple[str, ...]] | None = None,
    bounded: bool = False,
    semantic_reviewer: Callable[[list[ProposedBoundary]], Awaitable[_ReviewOutcome]] | None = None,
    max_candidates: int = 2,
    max_submissions: int = 4,
) -> Tool:
    """Capture the exact candidate only after deterministic and semantic validation."""

    validation_failures = 0
    candidates = 0
    submitted_fingerprints: set[str] = set()
    prior_issues: tuple[_ReviewIssue, ...] = ()

    def salvage_uncontested(
        boundaries: list[ProposedBoundary], issues: tuple[_ReviewIssue, ...]
    ) -> list[ProposedBoundary]:
        """Keep whole sibling moves that neither critic cited.

        A create-boundary candidate is a set of independent new sibling shelves. If a
        critic cites one target, discarding every other validated target prevents any
        progress. Salvage is deliberately narrow: one create operation, every finding
        must map to at least one complete move, and at least two untouched siblings must
        remain. Replace operations are indivisible and are never salvaged.
        """

        if len(boundaries) != 1 or boundaries[0].operation != "create_boundary" or not issues:
            return []
        boundary = boundaries[0]
        blocked: set[int] = set()
        for issue in issues:
            cited_paths = {
                handles[handle] for handle in issue.evidence_handles if handle in handles
            }
            matched = {
                index
                for index, move in enumerate(boundary.moves)
                if move.target in issue.problem
                or bool(cited_paths & {PurePosixPath(path) for path in move.paths})
            }
            if not matched:
                return []
            blocked.update(matched)
        kept = [move for index, move in enumerate(boundary.moves) if index not in blocked]
        if len(kept) < 2:
            return []
        return [
            ProposedBoundary(
                parent=boundary.parent,
                operation=boundary.operation,
                axis=boundary.axis,
                axis_question=boundary.axis_question,
                moves=kept,
            )
        ]

    async def _submit(args: _SubmitPlanArgs) -> str:
        nonlocal validation_failures, candidates, prior_issues
        if validation_failures >= max_submissions or candidates >= max_candidates:
            message = (
                f"Candidate limit reached ({max_candidates} reviewed candidates, "
                f"{max_submissions} validation failures). "
                "Call finish_no_change with the unresolved blocking reason."
            )
            log_trace(
                "agent.plan_limit_reached",
                scope=str(scope) or "/",
                max_candidates=max_candidates,
                max_submissions=max_submissions,
            )
            return message
        reviewed_this_call = False
        boundaries, problems = _validate_shadow_plan(
            vault,
            args,
            scope=scope,
            handles=handles,
            family_units=family_units,
            require_family_units=bool(family_units),
        )
        fingerprint = _candidate_fingerprint(boundaries) if not problems else ""
        if not problems and fingerprint in submitted_fingerprints:
            problems = ["the candidate repeats a previously rejected exact plan"]
        if not problems and prior_issues:
            unresolved = [
                issue.problem
                for issue in prior_issues
                if _finding_signature(
                    boundaries,
                    issue.evidence_handles,
                    handles=handles,
                    kind=issue.kind,
                )
                == issue.candidate_signature
            ]
            if unresolved:
                problems = [
                    "revised candidate leaves a prior blocking finding unchanged: " + problem
                    for problem in unresolved
                ]
        if not problems and semantic_reviewer is not None:
            try:
                outcome = await semantic_reviewer(boundaries)
            except Exception as exc:
                # Transport/context/tool failures are not semantic rejection.  In
                # particular, do not poison the candidate fingerprint or consume one
                # of the two reviewed-candidate slots: the identical plan must remain
                # retryable after an isolated critic recovers.
                sink.clear()
                problem_sink.clear()
                log_trace(
                    "agent.semantic_review_failed",
                    scope=str(scope) or "/",
                    candidate_fingerprint=fingerprint,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return (
                    "Semantic review could not complete due to an isolated reviewer "
                    f"failure ({type(exc).__name__}). The candidate was not rejected "
                    "and its fingerprint was rolled back; submit the same exact "
                    "candidate again."
                )
            candidates += 1
            reviewed_this_call = True
            problems = list(outcome.problems)
            prior_issues = outcome.issues if problems else ()
            if problems and (partial := salvage_uncontested(boundaries, outcome.issues)):
                removed = sum(len(boundary.moves) for boundary in boundaries) - sum(
                    len(boundary.moves) for boundary in partial
                )
                boundaries = partial
                problems = []
                prior_issues = ()
                log_trace(
                    "agent.plan_partially_accepted",
                    scope=str(scope) or "/",
                    removed_sibling_moves=removed,
                    accepted_sibling_moves=sum(len(boundary.moves) for boundary in boundaries),
                )
        if problems:
            if fingerprint:
                submitted_fingerprints.add(fingerprint)
            # Mechanical/sticky failures are bounded separately. They do not consume
            # the scarce semantic-review budget because no critic reviewed them.
            if not reviewed_this_call:
                validation_failures += 1
            sink.clear()
            problem_sink[:] = problems
            log_trace(
                "agent.plan_rejected",
                scope=str(scope) or "/",
                problems=problems,
                submitted=args.model_dump(mode="json"),
            )
            action = (
                "Do not submit another candidate; call finish_no_change with the blocking reason."
                if candidates >= max_candidates or validation_failures >= max_submissions
                else "Revise the complete exact candidate once and submit it again."
            )
            return "Plan rejected:\n- " + "\n- ".join(problems) + f"\n{action}"
        sink[:] = boundaries
        problem_sink.clear()
        move_count = sum(len(move.paths) for b in boundaries for move in b.moves)
        return f"Shadow plan accepted: {len(boundaries)} boundaries, {move_count} document moves."

    direct_children = {
        folder
        for folder in vault.iter_folders()
        if len(folder.parts) == len(scope.parts) + 1
        and folder.parts[: len(scope.parts)] == scope.parts
        and folder.parts[0] != INBOX.parts[0]
    }
    params: type[BaseModel] = _SubmitPlanArgs
    if bounded:
        charter_path = scope / CHARTER_FILENAME
        established = False
        if vault.exists(charter_path):
            try:
                charter = Charter.from_markdown(vault.read_text(charter_path), path=scope)
                established = bool(charter.split_basis and charter.split_question)
            except Exception:
                established = False
        if established:
            params = _SubmitIncrementalPlanArgs
        elif not direct_children:
            params = _SubmitInitialPlanArgs

    async def _submit_scoped(args: BaseModel) -> str:
        return await _submit(_SubmitPlanArgs.model_validate(args.model_dump(mode="python")))

    return FunctionTool(
        name="submit_plan",
        description=(
            "Submit the exact complete candidate. The host validates paths and membership, "
            "then isolated boundary and membership critics review this same exact object. "
            "A move target is normally just a direct child class name. It may be new: an "
            "accepted plan creates that shelf automatically. One add_sibling object may also "
            "repair current-window documents from an existing sibling. FAMILY_UNIT values are "
            "indivisible assignment handles; never submit their individual D members. At most "
            "two candidates are allowed."
        ),
        params=params,
        handler=_submit_scoped,
        read_only=True,
        concurrency_safe=False,
    )
