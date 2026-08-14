"""Filesystem planning and application for legacy deterministic subdivision."""

# ruff: noqa: E402, F401 -- mixins intentionally share the legacy service vocabulary


from __future__ import annotations

import asyncio
import logging
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TypeVar

from bismuth.domain.charter import CHARTER_FILENAME, Charter, boundary_purpose, routing_purpose
from bismuth.domain.document import DocumentCard, sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.maintenance import ProposedClass, normalise_label, validate_plan
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_trace
from bismuth.ports.catalog import Catalog
from bismuth.ports.llm import LLM, Prompt
from bismuth.ports.vault import INBOX, STATE_DIR, Vault
from bismuth.prompts import subdivision as prompts
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import read_sidecar_meta
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)

# Character budgeting is deliberately provider-neutral.  Tokenizers differ, but a
# 32k-character ceiling leaves a wide margin inside the smallest supported 65k-token
# context even with schema/tool framing.  Every maintenance call is built and measured
# before it reaches the adapter.
MAX_MAINTENANCE_PROMPT_CHARS = 32_000
PacketT = TypeVar("PacketT")
from bismuth.services.legacy.subdivision.helpers import (
    _boundary_wording_problem,
    _bounded_gather,
    _describe,
    _document_packets,
    _emerging_packets,
    _failed_boundary_checks,
    _failed_routing_checks,
    _free_filename,
    _groups_for_ids,
    _groups_relevant_to_ids,
    _in_inbox,
    _normalise,
    _normalise_sketch,
    _prompt_chars,
    _relevant_children,
    _same_axis,
    _same_name,
    _sketch_packets,
    _value_packets,
    _writing_system,
)
from bismuth.services.legacy.subdivision.models import Divided, _Contents


class SubdivisionApplicationMixin:

    def _existing_boundary_groups(
        self, folder: PurePosixPath, contents: _Contents

    ) -> list[prompts.Group]:
        """Describe the current direct children using the reviewed subtree handles."""
        direct_children: dict[str, str] = {}
        for child in self._vault.iter_folders():
            if child.parent != folder or _in_inbox(child):
                continue
            charter = self._charter(child)
            direct_children[child.name] = (
                boundary_purpose(parent.split_basis, child.name)
                if (parent := self._charter(folder)) is not None
                and parent.divided
                and (charter is None or charter.managed)
                else (
                    routing_purpose(charter.purpose, fallback=child.name)
                    if charter is not None
                    else ""
                )
            )

        members: dict[str, list[str]] = {name: [] for name in direct_children}
        for document_id, _, path in contents.documents:
            relative = path.relative_to(folder) if folder.parts else path
            if len(relative.parts) > 1 and relative.parts[0] in members:
                members[relative.parts[0]].append(document_id)
        return [
            prompts.Group(name=name, note=note, document_ids=members[name])
            for name, note in direct_children.items()
        ]

    def _route_existing(
        self,
        folder: PurePosixPath,
        contents: _Contents,
        plan: prompts.Division,
        charter: Charter | None,
    ) -> Divided:
        """Move loose documents behind existing direct signs in one transaction."""
        if charter is None or not charter.divided:
            return Divided(folder=folder)

        validation = validate_plan(
            axis=plan.basis,
            axis_question=plan.basis_question,
            groups=tuple(
                ProposedClass(name=group.name, document_ids=tuple(group.document_ids))
                for group in plan.groups
            ),
            available_document_ids=frozenset(
                document_id for document_id, _, _ in contents.documents
            ),
            allow_single_document=True,
            allow_no_division=True,
        )
        if not validation.accepted:
            return Divided(folder=folder)

        direct_names = {
            candidate.name
            for candidate in self._vault.iter_folders()
            if candidate.parent == folder and not _in_inbox(candidate)
        }
        targets: dict[str, PurePosixPath] = {}
        for group in plan.groups:
            if group.name not in direct_names:
                return Divided(folder=folder)
            target = folder / group.name
            if target.parent != folder or not self._vault.is_dir(target):
                return Divided(folder=folder)
            try:
                target_charter = self._charters.load(target)
            except BismuthError:
                return Divided(folder=folder)
            if target_charter is None or not target_charter.managed:
                return Divided(folder=folder)
            targets[group.name] = target

        operations: list[Operation] = []
        affected: list[PurePosixPath] = []
        moved = 0
        for group in plan.groups:
            target = targets[group.name]
            taken = {
                path.name.casefold() for path in self._vault.iter_files(target, recursive=False)
            }
            affected.append(target)
            for document_id in group.document_ids:
                source = contents.path_of(document_id)
                if source is None or source.parent != folder:
                    return Divided(folder=folder)
                filename = _free_filename(source.name, taken)
                taken.add(filename.casefold())
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=source,
                        target=target / filename,
                        note="route document behind existing sign",
                    )
                )
                source_sidecar = source.parent / sidecar_name(source.name)
                if self._vault.exists(source_sidecar):
                    operations.append(
                        Operation(
                            kind=OperationKind.MOVE,
                            source=source_sidecar,
                            target=target / sidecar_name(filename),
                            note="route sidecar behind existing sign",
                        )
                    )
                moved += 1

        if not operations:
            return Divided(folder=folder)
        note_operations, payloads = self._stable_child_note_operations(
            folder, axis=charter.split_basis
        )
        operations.extend(note_operations)
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"route {moved} documents through existing signs at {folder or '/'}",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        unique_affected = tuple(dict.fromkeys(affected))
        log_trace(
            "subdivide.routed_existing",
            folder=str(folder),
            targets=[str(target) for target in unique_affected],
            moved=moved,
            basis=plan.basis,
        )
        return Divided(
            folder=folder,
            created=unique_affected,
            moved=moved,
            basis=plan.basis,
        )

    def _apply(
        self,
        folder: PurePosixPath,
        contents: _Contents,
        plan: prompts.Division,
        charter: Charter | None,
    ) -> Divided:
        """Create the sub-folders, move the documents, write the notes. One entry."""
        available = frozenset(document_id for document_id, _, _ in contents.documents)
        validation = validate_plan(
            axis=plan.basis,
            axis_question=plan.basis_question,
            groups=tuple(
                ProposedClass(name=group.name, document_ids=tuple(group.document_ids))
                for group in plan.groups
            ),
            available_document_ids=available,
            ancestor_names=folder.parts,
            spent_axes=tuple(self._axes_above(folder)),
        )
        if not validation.accepted:
            reasons = [problem.value for problem in validation.problems]
            logger.info("division of %s rejected before apply: %s", folder or "/", reasons)
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="; ".join(reasons),
                proposed=[group.name for group in plan.groups],
            )
            return Divided(folder=folder)

        if problem := _boundary_wording_problem(contents, plan):
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason=problem,
                proposed=[group.name for group in plan.groups],
            )
            return Divided(folder=folder)

        # Path syntax is an adapter-facing concern, but it is still preflighted for the
        # whole proposal.  Never apply the valid half of an invalid model plan.
        try:
            names = [sanitize_segment(group.name) for group in plan.groups]
        except ValueError:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="invalid class name",
                proposed=[group.name for group in plan.groups],
            )
            return Divided(folder=folder)
        if len({name.casefold() for name in names}) != len(names):
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="class names collide after path sanitising",
                proposed=[group.name for group in plan.groups],
            )
            return Divided(folder=folder)
        if any(self._vault.exists(folder / name) for name in names):
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="proposed class already exists",
                proposed=[group.name for group in plan.groups],
            )
            return Divided(folder=folder)

        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        created: list[PurePosixPath] = []
        moved = 0

        for group, name in zip(plan.groups, names, strict=True):
            target = folder / name
            if target == folder or self._vault.exists(target):
                continue
            if _same_name(name, folder.parts):
                # A sub-folder has to distinguish something inside its ancestors, and one
                # carrying an ancestor's name distinguishes nothing. Check every ancestor,
                # not just the parent, because repeating a grandparent is equally useless.
                logger.info("division of %s proposed an ancestor's name; not a distinction", folder)
                log_trace(
                    "subdivide.rejected",
                    folder=str(folder),
                    reason="class carries an ancestor's name",
                    proposed=[group.name],
                )
                continue

            members = [
                (document_id, path)
                for document_id in group.document_ids
                if (path := contents.path_of(document_id)) is not None
            ]
            if not members:
                continue

            operations.append(Operation(kind=OperationKind.MKDIR, target=target))
            created.append(target)
            for _, path in members:
                operations.extend(self._move_document(path, target))
                moved += 1

            child_charter = Charter(
                path=target,
                title=name,
                purpose=boundary_purpose(plan.basis, name),
                holds=(),
                answers=(),
            )
            operations.append(
                Operation(
                    kind=OperationKind.WRITE, target=target / CHARTER_FILENAME, note="folder note"
                )
            )
            payloads[target / CHARTER_FILENAME] = child_charter.to_markdown().encode("utf-8")

        if not created:
            return Divided(folder=folder)

        if len(created) == 1 and moved == len(contents.documents):
            # One group holding everything distinguishes nothing -- it just moves the
            # whole folder a level deeper, and the level below is then the same problem
            # at the same size, for ever. A division has to divide.
            logger.info(
                "division of %s put every document in one group; not a division",
                folder or "/",
            )
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="single group took every document",
                proposed=[g.name for g in plan.groups],
            )
            return Divided(folder=folder)

        # The parent records what it was divided along, so the next look can ask whether
        # that still holds rather than starting from nothing.
        remaining = len(contents.documents) - moved
        note_operations, stable_payloads = self._stable_child_note_operations(
            folder, axis=plan.basis
        )
        operations.extend(note_operations)
        payloads.update(stable_payloads)
        parent = self._parent_note(
            folder, charter, plan, documents=self._count_documents(folder, recursive=True)
        )
        operations.append(
            Operation(
                kind=OperationKind.WRITE, target=folder / CHARTER_FILENAME, note="folder note"
            )
        )
        payloads[folder / CHARTER_FILENAME] = parent.to_markdown().encode("utf-8")

        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"divide {folder or '/'} into {len(created)} ({moved} document(s))",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        log_trace(
            "subdivide.applied",
            folder=str(folder),
            created=[str(c) for c in created],
            moved=moved,
            remaining=remaining,
            basis=plan.basis,
        )
        logger.info(
            "divided %s into %d folder(s), moved %d document(s)", folder or "/", len(created), moved
        )
        return Divided(
            folder=folder,
            created=tuple(created),
            moved=moved,
            basis=plan.basis,
        )

    def _replace_boundary(
        self,
        folder: PurePosixPath,
        plan: prompts.Division,
        charter: Charter | None,
    ) -> Divided:
        """Replace one complete subtree boundary as a single reversible transaction.

        Every document is staged before any old directory is retired. This avoids move
        cycles and filename collisions, and makes rollback independent of operation
        order. Existing direct child names may be reused, but their old nested shape is
        flattened; later arrivals can grow a new lower boundary from fresh evidence.
        """
        contents = self._read(folder, recursive=True)
        total = self._count_documents(folder, recursive=True)
        if len(contents.documents) != total or self._has_protected_descendant(folder):
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="subtree changed or contains state that maintenance cannot replace safely",
            )
            return Divided(folder=folder)

        available = frozenset(document_id for document_id, _, _ in contents.documents)
        validation = validate_plan(
            axis=plan.basis,
            axis_question=plan.basis_question,
            groups=tuple(
                ProposedClass(name=group.name, document_ids=tuple(group.document_ids))
                for group in plan.groups
            ),
            available_document_ids=available,
            ancestor_names=folder.parts,
            spent_axes=tuple(self._axes_above(folder)),
            require_complete=True,
        )
        if not validation.accepted:
            reasons = [problem.value for problem in validation.problems]
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="; ".join(reasons),
                proposed=[group.name for group in plan.groups],
                replacement=True,
            )
            return Divided(folder=folder)

        if problem := _boundary_wording_problem(contents, plan):
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason=problem,
                proposed=[group.name for group in plan.groups],
                replacement=True,
            )
            return Divided(folder=folder)

        try:
            names = [sanitize_segment(group.name) for group in plan.groups]
        except ValueError:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="invalid class name",
                proposed=[group.name for group in plan.groups],
                replacement=True,
            )
            return Divided(folder=folder)
        if len({name.casefold() for name in names}) != len(names):
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="class names collide after path sanitising",
                proposed=[group.name for group in plan.groups],
                replacement=True,
            )
            return Divided(folder=folder)


        stage = PurePosixPath(STATE_DIR) / f"boundary-{uuid.uuid4().hex[:12]}"
        operations: list[Operation] = [
            Operation(kind=OperationKind.MKDIR, target=stage, note="stage boundary replacement")
        ]
        payloads: dict[PurePosixPath, bytes] = {}
        staged: dict[str, tuple[PurePosixPath, PurePosixPath | None]] = {}

        for index, (document_id, _, source) in enumerate(contents.documents):
            staged_document = stage / f"{index:06d}-{source.name}"
            operations.append(
                Operation(
                    kind=OperationKind.MOVE,
                    source=source,
                    target=staged_document,
                    note="stage document for boundary replacement",
                )
            )
            source_sidecar = source.parent / sidecar_name(source.name)
            staged_sidecar: PurePosixPath | None = None
            if self._vault.exists(source_sidecar):
                staged_sidecar = stage / f"{index:06d}-{sidecar_name(source.name)}"
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=source_sidecar,
                        target=staged_sidecar,
                        note="stage sidecar for boundary replacement",
                    )
                )
            staged[document_id] = (staged_document, staged_sidecar)

        descendants = sorted(
            (
                candidate
                for candidate in self._vault.iter_folders()
                if candidate != folder
                and candidate.is_relative_to(folder)
                and not _in_inbox(candidate)
            ),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for descendant in descendants:
            note = descendant / CHARTER_FILENAME
            if self._vault.exists(note):
                operations.append(
                    Operation(kind=OperationKind.REMOVE, target=note, note="retire old folder note")
                )
            operations.append(
                Operation(kind=OperationKind.RMDIR, target=descendant, note="retire old boundary")
            )

        targets = [folder / name for name in names]
        for target in targets:
            operations.append(
                Operation(kind=OperationKind.MKDIR, target=target, note="replacement class")
            )

        for group, target in zip(plan.groups, targets, strict=True):
            taken: set[str] = set()
            for document_id in group.document_ids:
                staged_document, staged_sidecar = staged[document_id]
                filename = _free_filename(staged_document.name.split("-", 1)[1], taken)
                taken.add(filename.casefold())
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=staged_document,
                        target=target / filename,
                        note="place document under replacement boundary",
                    )
                )
                if staged_sidecar is not None:
                    operations.append(
                        Operation(
                            kind=OperationKind.MOVE,
                            source=staged_sidecar,
                            target=target / sidecar_name(filename),
                            note="place sidecar under replacement boundary",
                        )
                    )

            child_charter = Charter(
                path=target,
                title=target.name,
                purpose=boundary_purpose(plan.basis, target.name),
                holds=(),
                answers=(),
            )
            note_path = target / CHARTER_FILENAME
            operations.append(
                Operation(
                    kind=OperationKind.WRITE,
                    target=note_path,
                    note="replacement folder note",
                )
            )
            payloads[note_path] = child_charter.to_markdown().encode("utf-8")

        parent = self._parent_note(folder, charter, plan, documents=total)
        parent_note = folder / CHARTER_FILENAME
        operations.append(
            Operation(kind=OperationKind.WRITE, target=parent_note, note="replacement axis")
        )
        payloads[parent_note] = parent.to_markdown().encode("utf-8")
        operations.append(
            Operation(kind=OperationKind.RMDIR, target=stage, note="remove empty staging folder")
        )

        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"replace boundary of {folder or '/'} with {len(targets)} classes",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        log_trace(
            "subdivide.replaced",
            folder=str(folder),
            targets=[str(target) for target in targets],
            retired=[str(path) for path in descendants],
            moved=total,
            basis=plan.basis,
        )
        logger.info(
            "replaced boundary of %s with %d classes covering %d documents",
            folder or "/",
            len(targets),
            total,
        )
        return Divided(folder=folder, created=tuple(targets), moved=total, basis=plan.basis)

    def _rearm(
        self,
        folder: PurePosixPath,
        charter: Charter,
        *,
        documents: int,
        axis_question: str,
    ) -> None:
        """Record that the division was upheld at this size, so the next look waits."""
        held = charter.model_copy(
            update={
                "split_at_documents": documents,
                "split_question": axis_question,
                "boundary_review_required": not bool(axis_question.strip()),
                "last_review_at_documents": documents,
                "repair_pending": False,
            }
        )
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"division of {folder or '/'} still holds at {documents}",
                operations=(
                    Operation(
                        kind=OperationKind.WRITE,
                        target=folder / CHARTER_FILENAME,
                        note="folder note",
                    ),
                ),
            ),
            payloads={folder / CHARTER_FILENAME: held.to_markdown().encode("utf-8")},
        )

    def _record_review_attempt(
        self,
        folder: PurePosixPath,
        charter: Charter,
        *,
        documents: int,
        repair_pending: bool,
    ) -> None:
        """Persist a review outcome even when no safe structural mutation exists."""
        reviewed = charter.model_copy(
            update={
                "last_review_at_documents": documents,
                "repair_pending": repair_pending,
            }
        )
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"record boundary review of {folder or '/'} at {documents}",
                operations=(
                    Operation(
                        kind=OperationKind.WRITE,
                        target=folder / CHARTER_FILENAME,
                        note="folder review state",
                    ),
                ),
            ),
            payloads={folder / CHARTER_FILENAME: reviewed.to_markdown().encode("utf-8")},
        )

    def _parent_note(
        self,
        folder: PurePosixPath,
        charter: Charter | None,
        plan: prompts.Division,
        *,
        documents: int,
    ) -> Charter:
        title = charter.title if charter else (folder.name or "/")
        return Charter(
            path=folder,
            title=title,
            purpose=charter.purpose if charter else "",
            holds=(),
            answers=(),
            split_basis=plan.basis,
            split_question=plan.basis_question,
            split_at_documents=documents,
            last_review_at_documents=0,
            repair_pending=False,
        )

    def _stable_child_note_operations(
        self, folder: PurePosixPath, *, axis: str
    ) -> tuple[list[Operation], dict[PurePosixPath, bytes]]:
        """Migrate managed direct-child prose to deterministic structural signs."""
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        for child in self._vault.iter_folders():
            if child.parent != folder or _in_inbox(child):
                continue
            try:
                charter = self._charters.load(child)
            except BismuthError:
                continue
            if charter is None or not charter.managed:
                continue
            purpose = boundary_purpose(axis, child.name)
            if charter.purpose == purpose:
                continue
            stable = charter.model_copy(update={"title": child.name, "purpose": purpose})
            note = child / CHARTER_FILENAME
            operations.append(
                Operation(kind=OperationKind.WRITE, target=note, note="stabilise folder sign")
            )
            payloads[note] = stable.to_markdown().encode("utf-8")
        return operations, payloads

    def _move_document(self, path: PurePosixPath, target: PurePosixPath) -> list[Operation]:
        """Move a document and the sidecar that travels with it."""
        operations = [
            Operation(
                kind=OperationKind.MOVE, source=path, target=target / path.name, note="divide"
            )
        ]
        sidecar = path.parent / sidecar_name(path.name)
        if self._vault.exists(sidecar):
            operations.append(
                Operation(
                    kind=OperationKind.MOVE,
                    source=sidecar,
                    target=target / sidecar.name,
                    note="sidecar",
                )
            )
        return operations

    def _read(self, folder: PurePosixPath, *, recursive: bool = False) -> _Contents:
        """The folder as the model sees it, with a unique handle for every file."""
        contents = _Contents()
        for path in self._vault.iter_files(folder, recursive=recursive):
            if _in_inbox(path):
                continue
            card = self._card_of(path)
            if card is None:
                continue
            # Handles live only for this in-memory view.  The catalog's SHA-derived ID
            # remains the durable internal identity, but exposing it to a model wastes
            # tokens and makes exact copying fragile.  Paths carry the mapping needed to
            # execute the plan, so every maintenance prompt can use compact D#### names.
            document_id = f"D{len(contents.documents) + 1:04d}"
            description = _describe(card)
            if recursive:
                relative = path.relative_to(folder) if folder.parts else path
                description = f"current={relative} | {description}"
            contents.documents.append((document_id, description, path))
            if script := _writing_system(card.title):
                contents.scripts.append(script)

        for child in self._vault.iter_folders():
            if not child.parts or child == folder:
                continue
            if child.parts[0] == INBOX.parts[0]:
                continue
            if recursive:
                if not child.is_relative_to(folder):
                    continue
                shown = str(child.relative_to(folder) if folder.parts else child)
            else:
                if child.parent != folder:
                    continue
                shown = child.name
            note = ""
            try:
                if loaded := self._charters.load(child):
                    if (
                        loaded.managed
                        and (parent := self._charter(child.parent))
                        and parent.divided
                    ):
                        note = boundary_purpose(parent.split_basis, child.name)
                    else:
                        note = routing_purpose(loaded.purpose, fallback=child.name)
            except BismuthError:
                pass
            contents.children.append((shown, note))
        contents.children.sort(key=lambda item: (item[0].casefold(), item[1].casefold()))
        return contents

    def _has_protected_descendant(self, folder: PurePosixPath) -> bool:
        for candidate in self._vault.iter_folders():
            if candidate == folder or not candidate.is_relative_to(folder):
                continue
            if _in_inbox(candidate):
                continue
            try:
                charter = self._charters.load(candidate)
            except BismuthError:
                return True
            if charter is not None and not charter.managed:
                return True
        return False

    def _count_documents(self, folder: PurePosixPath, *, recursive: bool) -> int:
        return sum(
            1 for path in self._vault.iter_files(folder, recursive=recursive) if not _in_inbox(path)
        )

    def _card_of(self, path: PurePosixPath) -> DocumentCard | None:
        sidecar = path.parent / sidecar_name(path.name)
        if not self._vault.exists(sidecar):
            return None
        meta = read_sidecar_meta(self._vault.read_text(sidecar))
        if not meta:
            return None
        document_id = str(meta.get("document_id", ""))
        if not document_id:
            return None
        return self._catalog.load_card(document_id)

    def _axes_above(self, folder: PurePosixPath) -> list[str]:
        """The axes every folder from here to the root was divided along.

        They are spent: within this folder each of them has one constant value, so none
        of them can separate anything here.
        """
        axes: list[str] = []
        if not folder.parts:
            return axes
        current = folder.parent
        while True:
            charter = self._charter(current)
            if charter is not None and charter.split_basis:
                axes.append(charter.split_basis)
            if not current.parts:
                return axes
            current = current.parent

    def _charter(self, folder: PurePosixPath) -> Charter | None:
        try:
            return self._charters.load(folder)
        except BismuthError as exc:
            logger.warning("unreadable folder note at %s: %s", folder or "/", exc)
            return None
