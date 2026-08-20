"""Routing: putting a loose document behind a sign that already stands.

It creates nothing. Every folder it can choose was named and checked when it was drawn, so
the only judgement left is which of the standing signs this one document answers to, and
that is one closed choice per document (ADR-0014).

Asked only once nothing new has emerged. Run first, it drained the loose pile into the
shelves that already existed and a folder could never grow a third class: measured at one
root over 100 documents, 21 routings against 17 chances to name something new, and a width
frozen at two all run. The pile is the evidence a new class is drawn from, so it is read
for that first.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from bismuth.domain.charter import (
    Charter,
)
from bismuth.domain.document import sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.maintenance import (
    ProposedClass,
    validate_plan,
)
from bismuth.logging_setup import log_trace
from bismuth.prompts import subdivision as prompts
from bismuth.services.subdivision.naming import _free_filename, _in_inbox
from bismuth.services.subdivision.reading import (
    Divided,
    _bounded_gather,
    _Contents,
    _shown_fingerprint,
)
from bismuth.services.subdivision.shared import NeedsAFolder

logger = logging.getLogger(__name__)


class RoutesToAStandingSign(NeedsAFolder):
    """Putting a loose document behind a sign that already stands, creating nothing."""

    async def _existing_assignments(
        self,
        *,
        folder: PurePosixPath,
        contents: _Contents,
        charter: Charter,
    ) -> prompts.ExistingAssignments:
        merged: dict[str, list[str]] = {}
        handles = [f"F{index:03d}" for index in range(1, len(contents.children) + 1)]
        # What the signs looked like when a document last said STAY. A document that did
        # not belong behind any of these will not belong behind the same ones tomorrow,
        # and the pile it sits in only grows: 8,416 of these questions placed twelve
        # documents in one run, because every arrival asked the whole pile again.
        signs = _shown_fingerprint(contents.children)
        already = self._declined.setdefault((str(folder), signs), set())
        asking = [line for line in contents.lines if line[0] not in already]
        if not asking:
            return prompts.ExistingAssignments(groups=[])

        async def decide(document: tuple[str, str]) -> tuple[str, str]:
            choice = await self._llm.choose(
                prompts.build_existing_choice(
                    path=str(folder),
                    document=document,
                    axis=charter.split_basis,
                    axis_question=charter.split_question,
                    children=contents.children,
                ),
                choices=(*handles, "STAY"),
            )
            return document[0], choice

        for document_id, choice in await _bounded_gather(asking, decide):
            if choice in handles:
                merged.setdefault(choice, []).append(document_id)
            else:
                already.add(document_id)
        return prompts.ExistingAssignments(
            groups=[
                prompts.ExistingAssignment(folder_id=folder_id, document_ids=document_ids)
                for folder_id, document_ids in merged.items()
            ]
        )

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
        routed: list[tuple[str, PurePosixPath]] = []
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
                routed.append((document_id, target))
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
        self._log_moves(folder, routed)
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
            routed=True,
        )
