"""Dividing a folder once its contents show a distinction worth drawing.

The second half of filing (SPEC.md 3.4, ADR-0008). Placement answers "where in the
tree as it stands"; this answers "the tree as it stands is now wrong here". Without
it a first placement is permanent and the documents that arrived first decide the
shape of everything after them.

Normal growth reads cards sitting directly in one folder. A boundary review is rarer
and reads cards through that subtree because replacing a boundary without seeing the
books already behind its signs cannot be correct.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TypeVar

from bismuth.domain.charter import CHARTER_FILENAME, Charter, routing_purpose
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


@dataclass(frozen=True, slots=True)
class Divided:
    """What dividing one folder did."""

    folder: PurePosixPath
    created: tuple[PurePosixPath, ...] = ()
    moved: int = 0
    basis: str = ""

    @property
    def happened(self) -> bool:
        return bool(self.created) or self.moved > 0


@dataclass(slots=True)
class _Contents:
    """One folder as the model is shown it: cards, not documents."""

    documents: list[tuple[str, str, PurePosixPath]] = field(default_factory=list)
    """(document_id, one-line description, file path)."""
    children: list[tuple[str, str]] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    """Dominant writing system of each document title, when one is detectable."""

    @property
    def lines(self) -> list[tuple[str, str]]:
        return [(document_id, line) for document_id, line, _ in self.documents]

    def path_of(self, document_id: str) -> PurePosixPath | None:
        return next((p for i, _, p in self.documents if i == document_id), None)


class LibraryMaintenanceService:
    """Maintains the classification tree as evidence arrives.

    Placement shelves one document against the tree that exists.  This service owns
    changes to that tree: adding a class and reviewing or replacing an old boundary.
    Keeping the use cases separate makes a maintenance failure independent from a
    successfully filed document.
    """

    def __init__(
        self,
        *,
        vault: Vault,
        catalog: Catalog,
        charters: CharterService,
        transactor: Transactor,
        llm: LLM,
    ) -> None:
        self._vault = vault
        self._catalog = catalog
        self._charters = charters
        self._transactor = transactor
        self._llm = llm

    async def consider_with_ancestors(
        self,
        folder: PurePosixPath,
        *,
        filename: str = "",
        on_progress: ProgressSink | None = None,
    ) -> list[Divided]:
        """Consider the folder a document landed in, then every folder above it.

        Without the walk up, a top-level division is permanent. Once the root has
        children, documents land in the children and the root is never passed here
        again -- so the division it made when it held thirteen documents would still
        be its division at ten thousand. The ancestors are all divided already, so
        each is gated by the doubling rule and usually costs nothing.
        """
        results = await self.consider(folder, filename=filename, on_progress=on_progress)
        parent = folder.parent
        while folder.parts:
            # Nothing arrived directly in an ancestor, so its loose pile did not change.
            # Asking whether another class emerged there repeats the same question with
            # the same evidence.  The subtree did grow, so a due boundary review still
            # runs.  On the 300-document corpus this distinction removes hundreds of
            # redundant root calls without freezing the top-level axis.
            results.extend(
                await self.consider(
                    parent,
                    filename=filename,
                    on_progress=on_progress,
                    allow_emerging=False,
                )
            )
            folder = parent
            parent = folder.parent
        return results

    async def consider(
        self,
        folder: PurePosixPath,
        *,
        filename: str = "",
        on_progress: ProgressSink | None = None,
        allow_emerging: bool = True,
    ) -> list[Divided]:
        """Draw one class out of ``folder``, if one has grown in it.

        At most one folder is created per call, and never below the one it creates.

        ``filename`` is the document whose arrival prompted this, carried only so the
        progress events join that document's run rather than opening one of their own.
        """
        if folder.parts and folder.parts[0] == INBOX.parts[0]:
            return []  # the inbox holds what could not be read; it is not a category

        contents = self._read(folder)
        charter = self._charter(folder)

        if charter is not None and not charter.managed:
            # A human wrote this note; their structure is not ours to redraw. Traced
            # because "nothing happened here" should never need the source to explain.
            log_trace("subdivide.skipped", folder=str(folder), reason="folder note is not managed")
            return []

        plan = await self._judge(
            folder,
            contents,
            charter,
            filename=filename,
            on_progress=on_progress,
            allow_emerging=allow_emerging,
        )
        if plan is None or not plan.groups:
            return []

        divided = (
            self._replace_boundary(folder, plan, charter)
            if plan.replace_existing
            else (
                self._route_existing(folder, contents, plan, charter)
                if plan.reuse_existing
                else self._apply(folder, contents, plan, charter)
            )
        )
        if not divided.happened:
            return []

        # What was just created is not considered here. It was formed a moment ago from a
        # judgement over these same documents, and asking it again adds no evidence -- it
        # only re-judges. That recursion was worth having when a schedule could leave a
        # new folder unasked for a long time; every arrival asks now, so a child is
        # looked at as soon as anything lands in it. Measured with the recursion still
        # in: a single ingest built 철학/현상학/체화된 인지, one document per level.
        return [divided]

    async def _judge(
        self,
        folder: PurePosixPath,
        contents: _Contents,
        charter: Charter | None,
        *,
        filename: str,
        on_progress: ProgressSink | None,
        allow_emerging: bool,
    ) -> prompts.Division | None:
        """Ask the model. Returns None when there is nothing to ask about."""
        purpose = charter.purpose if charter else ""
        # Through the subtree: dividing moves this folder's documents into its children,
        # so a direct count collapses to nothing and the division is never looked at again.
        total = self._count_documents(folder, recursive=True)

        # Two different jobs, and only the second may move a document that is already
        # filed. Drawing a new class out of the loose pile is additive and safe to ask
        # often; redrawing a boundary is not, and waits for the evidence to double.
        if charter is not None and charter.divided and charter.due_for_review(total):
            review_contents = self._read(folder, recursive=True)
            if len(review_contents.documents) != total:
                log_trace(
                    "subdivide.skipped",
                    folder=str(folder),
                    reason="boundary review requires a card for every document",
                    documents=total,
                    cards=len(review_contents.documents),
                )
                return None
            if self._has_protected_descendant(folder):
                log_trace(
                    "subdivide.skipped",
                    folder=str(folder),
                    reason="boundary review contains a protected or unreadable descendant",
                )
                return None
            report(
                on_progress,
                Progress(stage=Stage.REVIEWING, filename=filename, note=str(folder) or "/"),
            )
            current_groups = self._existing_boundary_groups(folder, review_contents)
            direct_signs = [(group.name, group.note) for group in current_groups]
            review = await self._review_boundary(
                folder=folder,
                purpose=purpose,
                charter=charter,
                total=total,
                documents=review_contents.lines,
                children=direct_signs,
            )
            log_trace(
                "subdivide.review",
                folder=str(folder),
                basis=charter.split_basis,
                before=charter.split_at_documents,
                now=total,
                holds=review.holds,
                one_axis=review.one_axis,
                coherent_membership=review.coherent_membership,
                useful_navigation=review.useful_navigation,
            )
            # Schema upgrades force one complete semantic audit of every learned
            # boundary. Previously a failed legacy audit simply returned, leaving the
            # known-bad tree in place forever. A failed audit is evidence that the
            # boundary does not hold, so it must enter the same replacement path as a
            # failed review.
            current_boundary_holds = review.holds
            observed_failures = [
                name
                for name, passed in (
                    ("current signs do not follow one axis", review.one_axis),
                    ("current membership is incoherent", review.coherent_membership),
                    ("current signs do not improve navigation", review.useful_navigation),
                )
                if not passed
            ]
            if charter.boundary_review_required:
                current_audit = await self._audit_boundary(
                    folder=folder,
                    documents=review_contents.lines,
                    axis=charter.split_basis,
                    axis_question=charter.split_question,
                    groups=current_groups,
                    complete=True,
                )
                current_boundary_holds = current_boundary_holds and current_audit.accepted
                observed_failures.extend(_failed_boundary_checks(current_audit))
                log_trace(
                    "subdivide.current_boundary_audit",
                    folder=str(folder),
                    accepted=current_audit.accepted,
                    failed_checks=_failed_boundary_checks(current_audit),
                )

            if not current_boundary_holds:
                replacement = await self._propose_replacement(
                    folder=folder,
                    purpose=purpose,
                    charter=charter,
                    total=total,
                    documents=review_contents.lines,
                    children=direct_signs,
                )
                if replacement is None:
                    return None
                preview = validate_plan(
                    axis=replacement.basis,
                    axis_question=replacement.basis_question,
                    groups=tuple(
                        ProposedClass(name=group.name, document_ids=tuple(group.document_ids))
                        for group in replacement.groups
                    ),
                    available_document_ids=frozenset(
                        document_id for document_id, _, _ in review_contents.documents
                    ),
                    ancestor_names=folder.parts,
                    spent_axes=tuple(self._axes_above(folder)),
                    require_complete=True,
                )
                if not preview.accepted:
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="; ".join(problem.value for problem in preview.problems),
                        replacement=True,
                    )
                    return None
                audit = await self._audit_boundary(
                    folder=folder,
                    documents=review_contents.lines,
                    axis=replacement.basis,
                    axis_question=replacement.basis_question,
                    groups=replacement.groups,
                    complete=True,
                )
                if not audit.accepted:
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="semantic boundary audit failed",
                        failed_checks=_failed_boundary_checks(audit),
                        replacement=True,
                    )
                    return None
                change_audit = await self._audit_replacement_change(
                    folder=folder,
                    documents=review_contents.lines,
                    charter=charter,
                    current_groups=current_groups,
                    observed_failures=observed_failures,
                    replacement=replacement,
                )
                if not change_audit.accepted:
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="replacement is not materially better than current boundary",
                        fixes_observed_failure=change_audit.fixes_observed_failure,
                        better_navigation=change_audit.better_navigation,
                        replacement=True,
                    )
                    return None
                return prompts.Division(
                    basis=replacement.basis,
                    basis_question=replacement.basis_question,
                    groups=replacement.groups,
                    replace_existing=True,
                )
            # A holding review is still a judgement made at this size, and it has to be
            # recorded as one. Left unwritten, the folder stays past its doubling for
            # ever: it was asked on every ingest from then on -- fourteen times in a row
            # on one run, all of them holding -- and, worse, the answer returned here so
            # the folder was never asked what else had grown in it.
            self._rearm(
                folder,
                charter,
                documents=total,
                axis_question=charter.split_question,
            )

        if not allow_emerging:
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                reason="ancestor loose pile unchanged",
            )
            return None

        if not contents.documents:
            log_trace("subdivide.skipped", folder=str(folder), reason="nothing sitting here")
            return None

        report(
            on_progress, Progress(stage=Stage.DIVIDING, filename=filename, note=str(folder) or "/")
        )

        if (
            charter is not None
            and charter.divided
            and charter.split_basis
            and charter.split_question
            and contents.children
        ):
            assignments = await self._existing_assignments(
                folder=folder, contents=contents, charter=charter
            )
            log_trace(
                "subdivide.existing_assignments",
                folder=str(folder),
                groups=[group.folder_id for group in assignments.groups],
                claimed=sum(len(group.document_ids) for group in assignments.groups),
            )
            if assignments.groups:
                child_handles = {
                    f"F{index:03d}": (name, note)
                    for index, (name, note) in enumerate(contents.children, start=1)
                }
                unknown_handles = sorted(
                    {
                        group.folder_id.strip().upper()
                        for group in assignments.groups
                        if group.folder_id.strip().upper() not in child_handles
                    }
                )
                if unknown_handles:
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="unknown existing folder handle",
                        proposed=unknown_handles,
                    )
                    assignments = prompts.ExistingAssignments(groups=[])
                resolved_groups = [
                    prompts.Group(
                        name=child_handles[group.folder_id.strip().upper()][0],
                        note=child_handles[group.folder_id.strip().upper()][1],
                        document_ids=group.document_ids,
                    )
                    for group in assignments.groups
                ]
            else:
                resolved_groups = []
            if resolved_groups:
                preview = validate_plan(
                    axis=charter.split_basis,
                    axis_question=charter.split_question,
                    groups=tuple(
                        ProposedClass(name=group.name, document_ids=tuple(group.document_ids))
                        for group in resolved_groups
                    ),
                    available_document_ids=frozenset(
                        document_id for document_id, _, _ in contents.documents
                    ),
                    allow_single_document=True,
                    allow_no_division=True,
                )
                if preview.accepted:
                    routing_audit = await self._audit_routing(
                        folder=folder,
                        documents=contents.lines,
                        charter=charter,
                        groups=resolved_groups,
                        children=contents.children,
                    )
                    if routing_audit.accepted:
                        return prompts.Division(
                            basis=charter.split_basis,
                            basis_question=charter.split_question,
                            groups=resolved_groups,
                            reuse_existing=True,
                        )
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="existing assignment audit failed",
                        failed_checks=_failed_routing_checks(routing_audit),
                    )
                else:
                    reasons = [problem.value for problem in preview.problems]
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="; ".join(reasons),
                        proposed=[group.name for group in resolved_groups],
                    )

        # Asked on every arrival, and it is the arrival that makes it worth asking. A
        # power-of-two schedule was tried: the root of a thirty-document archive was asked
        # at 2, 4, 8 and 16, declined all four -- correctly, the classes had not gathered
        # yet -- and then waited for a thirty-second document that never came. Fourteen
        # documents arrived unasked and nothing was ever filed. The schedule was built for
        # the old question, "how would you divide this", which has an answer every time
        # and so slipped into yes if asked often enough. This one declines and keeps
        # declining, so there is nothing to ration.
        #
        # One class at a time, never a partition. A heterogeneous pile cannot honestly
        # account for every document without inventing a remainder class. Nothing in this
        # schema can express "the rest".
        # The axis this folder was divided along, if it has been. Every sub-folder here
        # is one answer to it, and a later class has to answer the same question --
        # otherwise the siblings sit on different distinctions and no name rules anything
        # out.
        axis = charter.split_basis if charter is not None else ""
        spent = self._axes_above(folder)

        emerging = await self._find_emerging(
            folder=folder,
            purpose=purpose,
            documents=contents.lines,
            children=contents.children,
            axis=axis,
            spent=spent,
        )
        log_trace(
            "subdivide.emerging",
            folder=str(folder),
            documents=len(contents.documents),
            subtree=total,
            axis=axis or emerging.axis,
            axis_is_new=not axis,
            emerged=emerging.emerged,
            name=emerging.name,
        )
        if not emerging.emerged or not emerging.name.strip():
            return None

        if normalise_label(emerging.name) in {
            normalise_label(name) for name, _ in contents.children
        }:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="proposed class already exists as a direct child",
                proposed=[emerging.name],
            )
            return None

        proposed = emerging.axis.strip()
        if not axis and proposed and _same_axis(proposed, spent):
            # Everything in this folder shares the ancestors' answer to their axes -- that
            # is what put these documents together -- so those axes cannot tell any of
            # them apart. Reusing one creates a repeated, non-distinguishing boundary.
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="axis already used above here",
                proposed=[proposed],
                spent=spent,
            )
            return None

        members = await self._find_members(
            folder=folder,
            purpose=purpose,
            documents=contents.lines,
            children=contents.children,
            name=emerging.name,
            note=emerging.note,
        )
        log_trace(
            "subdivide.members",
            folder=str(folder),
            name=emerging.name,
            claimed=len(members.document_ids),
            of=len(contents.documents),
        )
        if not members.document_ids:
            return None

        proposed_groups = [
            prompts.Group(
                name=emerging.name,
                note=routing_purpose(emerging.note, fallback=emerging.name),
                document_ids=members.document_ids,
            )
        ]
        preview = validate_plan(
            axis=axis or emerging.axis.strip(),
            axis_question=(
                charter.split_question
                if charter is not None and charter.split_question
                else emerging.axis_question
            ),
            groups=tuple(
                ProposedClass(name=group.name, document_ids=tuple(group.document_ids))
                for group in proposed_groups
            ),
            available_document_ids=frozenset(
                document_id for document_id, _, _ in contents.documents
            ),
            ancestor_names=folder.parts,
            spent_axes=tuple(spent),
        )
        if not preview.accepted:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="; ".join(problem.value for problem in preview.problems),
                proposed=[group.name for group in proposed_groups],
            )
            return None
        audit_documents = contents.lines
        audit_groups = proposed_groups
        if charter is not None and charter.divided and contents.children:
            # A new shelf changes the meaning of the whole list of signs. Checking it
            # alone cannot detect an overlapping sibling, a mixed axis, or another
            # useless intermediate layer. Audit the proposed answer together with every
            # existing direct child against the full subtree.
            review_contents = self._read(folder, recursive=True)
            if len(review_contents.documents) != total or self._has_protected_descendant(folder):
                log_trace(
                    "subdivide.rejected",
                    folder=str(folder),
                    reason="full sibling audit requires every card and managed descendant",
                    documents=total,
                    cards=len(review_contents.documents),
                )
                return None

            recursive_id_by_path = {
                path: document_id for document_id, _, path in review_contents.documents
            }
            converted_groups: list[prompts.Group] = []
            for group in proposed_groups:
                converted_ids: list[str] = []
                for document_id in group.document_ids:
                    path = contents.path_of(document_id)
                    recursive_id = recursive_id_by_path.get(path) if path is not None else None
                    if recursive_id is None:
                        log_trace(
                            "subdivide.rejected",
                            folder=str(folder),
                            reason="proposed member could not be mapped into sibling audit",
                            proposed=[group.name],
                        )
                        return None
                    converted_ids.append(recursive_id)
                converted_groups.append(
                    prompts.Group(
                        name=group.name,
                        note=group.note,
                        document_ids=converted_ids,
                    )
                )
            audit_documents = review_contents.lines
            audit_groups = self._existing_boundary_groups(folder, review_contents)
            audit_groups.extend(converted_groups)

        audit = await self._audit_boundary(
            folder=folder,
            documents=audit_documents,
            axis=axis or emerging.axis.strip(),
            axis_question=(
                charter.split_question
                if charter is not None and charter.split_question
                else emerging.axis_question
            ),
            groups=audit_groups,
            complete=False,
        )
        if not audit.accepted:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="semantic boundary audit failed",
                failed_checks=_failed_boundary_checks(audit),
            )
            return None

        return prompts.Division(
            # The axis, not a sentence about this one extraction. It is read back on the
            # next look and on review, and it is what holds the siblings to one question.
            basis=axis or emerging.axis.strip() or emerging.name,
            basis_question=(
                charter.split_question
                if charter is not None and charter.split_question
                else emerging.axis_question
            ),
            groups=proposed_groups,
        )

    async def _existing_assignments(
        self,
        *,
        folder: PurePosixPath,
        contents: _Contents,
        charter: Charter,
    ) -> prompts.ExistingAssignments:
        def build(packet: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
            return prompts.build_existing_assignments(
                path=str(folder),
                documents=packet,
                axis=charter.split_basis,
                axis_question=charter.split_question,
                children=contents.children,
            )

        if _prompt_chars(build([])) > MAX_MAINTENANCE_PROMPT_CHARS:
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                reason="direct signs require boundary review before incremental routing",
            )
            return prompts.ExistingAssignments(groups=[])

        merged: dict[str, list[str]] = {}
        for packet in _document_packets(contents.lines, build):
            result = await self._llm.structured(
                build(packet),
                schema=prompts.ExistingAssignments,
            )
            available = {document_id for document_id, _ in packet}
            for group in result.groups:
                valid_ids = [
                    document_id for document_id in group.document_ids if document_id in available
                ]
                merged.setdefault(group.folder_id, []).extend(valid_ids)
        return prompts.ExistingAssignments(
            groups=[
                prompts.ExistingAssignment(folder_id=folder_id, document_ids=document_ids)
                for folder_id, document_ids in merged.items()
            ]
        )

    async def _audit_routing(
        self,
        *,
        folder: PurePosixPath,
        documents: list[tuple[str, str]],
        charter: Charter,
        groups: list[prompts.Group],
        children: list[tuple[str, str]],
    ) -> prompts.RoutingAudit:
        def build(packet: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
            ids = {document_id for document_id, _ in packet}
            packet_groups = [
                group.model_copy(
                    update={
                        "document_ids": [
                            document_id for document_id in group.document_ids if document_id in ids
                        ]
                    }
                )
                for group in groups
            ]
            return prompts.build_routing_audit(
                path=str(folder),
                documents=packet,
                axis=charter.split_basis,
                axis_question=charter.split_question,
                groups=packet_groups,
                children=children,
            )

        checks = [
            await self._llm.structured(build(packet), schema=prompts.RoutingAudit)
            for packet in _document_packets(documents, build)
        ]
        return prompts.RoutingAudit(
            assignments_match_signs=all(check.assignments_match_signs for check in checks),
            no_forced_fit=all(check.no_forced_fit for check in checks),
        )

    async def _find_emerging(
        self,
        *,
        folder: PurePosixPath,
        purpose: str,
        documents: list[tuple[str, str]],
        children: list[tuple[str, str]],
        axis: str,
        spent: list[str],
    ) -> prompts.Emerging:
        def build(packet: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
            return prompts.build_emerging(
                path=str(folder),
                purpose=purpose,
                documents=packet,
                children=children,
                axis=axis,
                spent=spent,
            )

        if _prompt_chars(build([])) > MAX_MAINTENANCE_PROMPT_CHARS:
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                reason="direct signs require boundary review before another class can emerge",
            )
            return prompts.Emerging(emerged=False)

        packets = _document_packets(documents, build)
        if len(packets) == 1:
            return await self._llm.structured(build(packets[0]), schema=prompts.Emerging)
        candidates = [
            await self._llm.structured(build(packet), schema=prompts.Emerging) for packet in packets
        ]
        candidates = [candidate for candidate in candidates if candidate.emerged]
        if not candidates:
            return prompts.Emerging(emerged=False)
        while len(candidates) > 1:
            batches = _emerging_packets(
                folder=folder,
                purpose=purpose,
                axis=axis,
                children=children,
                candidates=candidates,
            )
            if all(len(batch) == 1 for batch in batches):
                return prompts.Emerging(emerged=False)
            reduced: list[prompts.Emerging] = []
            for batch in batches:
                if len(batch) == 1:
                    reduced.extend(batch)
                else:
                    reduced.append(
                        await self._llm.structured(
                            prompts.build_emerging_reduce(
                                path=str(folder),
                                purpose=purpose,
                                axis=axis,
                                children=children,
                                candidates=batch,
                            ),
                            schema=prompts.Emerging,
                        )
                    )
            candidates = [candidate for candidate in reduced if candidate.emerged]
            if not candidates:
                return prompts.Emerging(emerged=False)
        return candidates[0]

    async def _find_members(
        self,
        *,
        folder: PurePosixPath,
        purpose: str,
        documents: list[tuple[str, str]],
        children: list[tuple[str, str]],
        name: str,
        note: str,
    ) -> prompts.Members:
        def build(packet: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
            return prompts.build_members(
                path=str(folder),
                purpose=purpose,
                documents=packet,
                children=children,
                name=name,
                note=note,
            )

        members: list[str] = []
        for packet in _document_packets(documents, build):
            result = await self._llm.structured(build(packet), schema=prompts.Members)
            available = {document_id for document_id, _ in packet}
            members.extend(
                document_id for document_id in result.document_ids if document_id in available
            )
        return prompts.Members(document_ids=list(dict.fromkeys(members)))

    async def _review_boundary(
        self,
        *,
        folder: PurePosixPath,
        purpose: str,
        charter: Charter,
        total: int,
        documents: list[tuple[str, str]],
        children: list[tuple[str, str]],
    ) -> prompts.Review:
        """Review every document through isolated packets and reduce with boolean AND."""

        def build(packet: list[tuple[str, str]], signs: list[tuple[str, str]] = children) -> Prompt:
            return prompts.build_review(
                path=str(folder),
                purpose=purpose,
                basis=charter.split_basis,
                basis_question=charter.split_question,
                before=charter.split_at_documents,
                count=total,
                documents=packet,
                children=signs,
            )

        checks: list[prompts.Review] = []
        sign_packets: list[list[tuple[str, str]]] = []
        if _prompt_chars(build([], children)) > MAX_MAINTENANCE_PROMPT_CHARS:
            sign_packets = _value_packets(children, lambda signs: build([], signs))
            document_packets = _document_packets(
                documents,
                lambda packet: build(packet, _relevant_children(packet, children)),
            )
        else:
            document_packets = _document_packets(documents, build)

        for signs in sign_packets:
            checks.append(await self._llm.structured(build([], signs), schema=prompts.Review))
        for index, packet in enumerate(document_packets, start=1):
            signs = _relevant_children(packet, children) if sign_packets else children
            checks.append(await self._llm.structured(build(packet, signs), schema=prompts.Review))
            log_trace(
                "subdivide.review_packet",
                folder=str(folder),
                packet=index,
                packets=len(document_packets),
                documents=len(packet),
            )
        return prompts.Review(
            one_axis=all(check.one_axis for check in checks),
            coherent_membership=all(check.coherent_membership for check in checks),
            useful_navigation=all(check.useful_navigation for check in checks),
        )

    async def _propose_replacement(
        self,
        *,
        folder: PurePosixPath,
        purpose: str,
        charter: Charter,
        total: int,
        documents: list[tuple[str, str]],
        children: list[tuple[str, str]],
    ) -> prompts.Replacement | None:
        """Build a complete replacement without ever placing the subtree in one context.

        Small boundaries retain the original single-call contract. Large ones first make
        membership-free sketches in isolated contexts, reduce those sketches, then assign
        every request-local document handle in bounded packets. Completeness remains a
        deterministic invariant before any filesystem transaction is constructed.
        """
        full = prompts.build_replacement(
            path=str(folder),
            purpose=purpose,
            basis=charter.split_basis,
            basis_question=charter.split_question,
            before=charter.split_at_documents,
            count=total,
            documents=documents,
            children=children,
        )
        if _prompt_chars(full) <= MAX_MAINTENANCE_PROMPT_CHARS:
            replacement = await self._llm.structured(full, schema=prompts.Replacement)
            return _normalise_replacement(replacement)

        def build_sketch(
            packet: list[tuple[str, str]], signs: list[tuple[str, str]] = children
        ) -> Prompt:
            return prompts.build_replacement_sketch(
                path=str(folder),
                purpose=purpose,
                current_axis=charter.split_basis,
                current_question=charter.split_question,
                documents=packet,
                children=signs,
            )

        sign_packets: list[list[tuple[str, str]]] = []
        if _prompt_chars(build_sketch([], children)) > MAX_MAINTENANCE_PROMPT_CHARS:
            sign_packets = _value_packets(children, lambda signs: build_sketch([], signs))
            document_packets = _document_packets(
                documents,
                lambda packet: build_sketch(packet, _relevant_children(packet, children)),
            )
        else:
            document_packets = _document_packets(documents, build_sketch)
        sketches: list[prompts.ReplacementSketch] = []
        for signs in sign_packets:
            sketch = await self._llm.structured(
                build_sketch([], signs),
                schema=prompts.ReplacementSketch,
            )
            sketches.append(_normalise_sketch(sketch))
        for index, packet in enumerate(document_packets, start=1):
            signs = _relevant_children(packet, children) if sign_packets else children
            sketch = await self._llm.structured(
                build_sketch(packet, signs),
                schema=prompts.ReplacementSketch,
            )
            sketches.append(_normalise_sketch(sketch))
            log_trace(
                "subdivide.replacement_sketch",
                folder=str(folder),
                packet=index,
                packets=len(document_packets),
                documents=len(packet),
            )

        while len(sketches) > 1:
            batches = _sketch_packets(folder, sketches)
            if all(len(batch) == 1 for batch in batches):
                log_trace(
                    "subdivide.rejected",
                    folder=str(folder),
                    reason="replacement sketches cannot fit a bounded reduce context",
                    replacement=True,
                )
                return None
            reduced: list[prompts.ReplacementSketch] = []
            for batch in batches:
                if len(batch) == 1:
                    reduced.extend(batch)
                    continue
                result = await self._llm.structured(
                    prompts.build_replacement_reduce(path=str(folder), sketches=batch),
                    schema=prompts.ReplacementSketch,
                )
                reduced.append(_normalise_sketch(result))
            sketches = reduced

        sketch = sketches[0]

        def build_assignments(packet: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
            return prompts.build_replacement_assignments(
                path=str(folder), documents=packet, sketch=sketch
            )

        assignments_by_sign: list[list[str]] = [[] for _ in sketch.signs]
        for index, packet in enumerate(_document_packets(documents, build_assignments), start=1):
            assigned = await self._llm.structured(
                build_assignments(packet),
                schema=prompts.ReplacementAssignments,
            )
            expected = {document_id for document_id, _ in packet}
            seen: set[str] = set()
            invalid = bool(assigned.unassigned_document_ids)
            for group in assigned.groups:
                handle = group.folder_id
                if not handle.startswith("G") or not handle[1:].isdigit():
                    invalid = True
                    continue
                sign_index = int(handle[1:]) - 1
                if sign_index < 0 or sign_index >= len(sketch.signs):
                    invalid = True
                    continue
                for document_id in group.document_ids:
                    if document_id not in expected or document_id in seen:
                        invalid = True
                    else:
                        seen.add(document_id)
                        assignments_by_sign[sign_index].append(document_id)
            if invalid or seen != expected:
                log_trace(
                    "subdivide.rejected",
                    folder=str(folder),
                    reason="bounded replacement assignment is incomplete or invalid",
                    packet=index,
                    replacement=True,
                )
                return None

        return prompts.Replacement(
            basis=sketch.basis,
            basis_question=sketch.basis_question,
            groups=[
                prompts.Group(
                    name=sign.name,
                    note=routing_purpose(sign.note, fallback=sign.name),
                    document_ids=assignments_by_sign[index],
                )
                for index, sign in enumerate(sketch.signs)
            ],
        )

    async def _audit_boundary(
        self,
        *,
        folder: PurePosixPath,
        documents: list[tuple[str, str]],
        axis: str,
        axis_question: str,
        groups: list[prompts.Group],
        complete: bool,
    ) -> prompts.BoundaryAudit:
        def build(
            packet: list[tuple[str, str]], shown_groups: list[prompts.Group] | None = None
        ) -> Prompt:
            ids = {document_id for document_id, _ in packet}
            source_groups = shown_groups if shown_groups is not None else groups
            packet_groups = _groups_for_ids(source_groups, ids)
            return prompts.build_boundary_audit(
                path=str(folder),
                documents=packet,
                axis=axis,
                axis_question=axis_question,
                groups=packet_groups,
                complete=complete,
            )

        checks: list[prompts.BoundaryAudit] = []
        if _prompt_chars(build([], groups)) > MAX_MAINTENANCE_PROMPT_CHARS:
            group_packets = _value_packets(groups, lambda shown: build([], shown))
            for shown in group_packets:
                checks.append(
                    await self._llm.structured(
                        build([], shown),
                        schema=prompts.BoundaryAudit,
                    )
                )
            document_packets = _document_packets(
                documents,
                lambda packet: build(
                    packet,
                    _groups_relevant_to_ids(groups, {item[0] for item in packet}),
                ),
            )
            for packet in document_packets:
                shown = _groups_relevant_to_ids(groups, {item[0] for item in packet})
                checks.append(
                    await self._llm.structured(
                        build(packet, shown),
                        schema=prompts.BoundaryAudit,
                    )
                )
        else:
            checks = [
                await self._llm.structured(build(packet), schema=prompts.BoundaryAudit)
                for packet in _document_packets(documents, build)
            ]
        return prompts.BoundaryAudit(
            one_property=all(check.one_property for check in checks),
            names_answer_question=all(check.names_answer_question for check in checks),
            mutually_exclusive=all(check.mutually_exclusive for check in checks),
            useful_for_navigation=all(check.useful_for_navigation for check in checks),
            notes_are_routing_signs=all(check.notes_are_routing_signs for check in checks),
        )

    async def _audit_replacement_change(
        self,
        *,
        folder: PurePosixPath,
        documents: list[tuple[str, str]],
        charter: Charter,
        current_groups: list[prompts.Group],
        observed_failures: list[str],
        replacement: prompts.Replacement,
    ) -> prompts.ReplacementAudit:
        current_children = [(group.name, group.note) for group in current_groups]

        def build(
            packet: list[tuple[str, str]],
            shown_current: list[tuple[str, str]] = current_children,
        ) -> Prompt:
            ids = {document_id for document_id, _ in packet}
            proposed = [
                group.model_copy(
                    update={
                        "document_ids": [
                            document_id for document_id in group.document_ids if document_id in ids
                        ]
                    }
                )
                for group in replacement.groups
            ]
            return prompts.build_replacement_audit(
                path=str(folder),
                documents=packet,
                current_axis=charter.split_basis,
                current_question=charter.split_question,
                current_children=shown_current,
                observed_failures=observed_failures,
                proposed_axis=replacement.basis,
                proposed_question=replacement.basis_question,
                proposed_groups=proposed,
            )

        checks: list[prompts.ReplacementAudit] = []
        if _prompt_chars(build([], current_children)) > MAX_MAINTENANCE_PROMPT_CHARS:
            for shown in _value_packets(current_children, lambda signs: build([], signs)):
                checks.append(
                    await self._llm.structured(
                        build([], shown),
                        schema=prompts.ReplacementAudit,
                    )
                )
            document_packets = _document_packets(
                documents,
                lambda packet: build(packet, _relevant_children(packet, current_children)),
            )
            for packet in document_packets:
                checks.append(
                    await self._llm.structured(
                        build(packet, _relevant_children(packet, current_children)),
                        schema=prompts.ReplacementAudit,
                    )
                )
        else:
            checks = [
                await self._llm.structured(
                    build(packet),
                    schema=prompts.ReplacementAudit,
                )
                for packet in _document_packets(documents, build)
            ]
        return prompts.ReplacementAudit(
            fixes_observed_failure=all(check.fixes_observed_failure for check in checks),
            better_navigation=all(check.better_navigation for check in checks),
        )

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
                routing_purpose(charter.purpose, fallback=child.name) if charter is not None else ""
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
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"route {moved} documents through existing signs at {folder or '/'}",
                operations=tuple(operations),
            )
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
                purpose=routing_purpose(group.note, fallback=name),
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
                purpose=routing_purpose(group.note, fallback=target.name),
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
        )

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
        seen_ids: dict[str, int] = {}
        for path in self._vault.iter_files(folder, recursive=recursive):
            if _in_inbox(path):
                continue
            raw_id, card = self._card_of(path)
            if card is None:
                continue
            if recursive:
                # Complete reviews need handles only for this one proposal. Compact,
                # deterministic handles cost fewer tokens and are much harder for a model
                # to mistype than opaque content hashes.
                document_id = f"D{len(contents.documents) + 1:04d}"
            else:
                occurrence = seen_ids.get(raw_id, 0) + 1
                seen_ids[raw_id] = occurrence
                document_id = raw_id if occurrence == 1 else f"{raw_id}~{occurrence}"
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

    def _card_of(self, path: PurePosixPath) -> tuple[str, DocumentCard | None]:
        sidecar = path.parent / sidecar_name(path.name)
        if not self._vault.exists(sidecar):
            return "", None
        meta = read_sidecar_meta(self._vault.read_text(sidecar))
        if not meta:
            return "", None
        document_id = str(meta.get("document_id", ""))
        if not document_id:
            return "", None
        return document_id, self._catalog.load_card(document_id)

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


def _normalise(text: str) -> str:
    return "".join(text.split()).casefold()


def _same_name(name: str, ancestors: tuple[str, ...]) -> bool:
    return any(_normalise(name) == _normalise(part) for part in ancestors)


def _same_axis(proposed: str, spent: list[str]) -> bool:
    """Whether an axis has already been used somewhere above."""
    wanted = normalise_label(proposed)
    return any(wanted == normalise_label(used) for used in spent)


def _writing_system(text: str) -> str | None:
    """Return the dominant Unicode writing system in ``text``, when it is clear.

    This deliberately identifies scripts rather than languages.  The library can be
    handed any corpus, so a list of Korean legal terms (or English medical terms) would
    be an application-specific heuristic.  Unicode script names let us catch a model
    unexpectedly translating all of its signs while leaving mixed-language collections
    and ordinary borrowed words alone.
    """
    counts: Counter[str] = Counter()
    for character in text:
        if not character.isalpha():
            continue
        name = unicodedata.name(character, "")
        if any(
            marker in name
            for marker in ("HANGUL", "CJK UNIFIED", "IDEOGRAPH", "HIRAGANA", "KATAKANA")
        ):
            counts["east-asian"] += 1
            continue
        for marker, script in (
            ("LATIN", "latin"),
            ("CYRILLIC", "cyrillic"),
            ("ARABIC", "arabic"),
            ("HEBREW", "hebrew"),
            ("DEVANAGARI", "devanagari"),
            ("THAI", "thai"),
            ("GREEK", "greek"),
        ):
            if marker in name:
                counts[script] += 1
                break

    total = sum(counts.values())
    if not total:
        return None
    script, count = counts.most_common(1)[0]
    return script if count / total >= 0.60 else None


def _boundary_wording_problem(contents: _Contents, plan: prompts.Division) -> str | None:
    """Reject unusable sign wording before it can become filesystem structure.

    Semantic correctness still belongs to the model audits.  These are mechanical UX
    invariants: a folder note is a short routing hint, and a proposal must not silently
    translate a corpus whose own writing system is unambiguous.
    """
    for group in plan.groups:
        note = routing_purpose(group.note, fallback=group.name)
        if "\n" in note or "\r" in note:
            return "folder note must be a short, single-line routing hint"

    if len(contents.scripts) < 2:
        return None
    source_counts = Counter(contents.scripts)
    source_script, source_count = source_counts.most_common(1)[0]
    if source_count / len(contents.scripts) < 0.75:
        return None

    wording = " ".join(
        [plan.basis, plan.basis_question]
        + [part for group in plan.groups for part in (group.name, group.note)]
    )
    proposed_script = _writing_system(wording)
    if proposed_script is not None and proposed_script != source_script:
        return "boundary wording uses a different writing system from its documents"
    return None


# Compatibility for embedders that used the alpha API. New code should name the role,
# not the one operation the first implementation happened to support.
SubdivisionService = LibraryMaintenanceService


def _describe(card: DocumentCard) -> str:
    """The card evidence used for grouping; never the original document bytes."""
    topics = ", ".join(card.topics)
    parts = [card.title, card.doc_type]
    if topics:
        parts.append(topics)
    if card.summary:
        parts.append(card.summary)
    return " | ".join(parts)


def _prompt_chars(prompt: Prompt) -> int:
    return len(prompt.system) + len(prompt.user)


def _document_packets(
    documents: list[tuple[str, str]],
    build: Callable[[list[tuple[str, str]]], Prompt],
) -> list[list[tuple[str, str]]]:
    """Partition evidence by the prompt actually sent, never by guessed token counts."""
    if not documents:
        if _prompt_chars(build([])) > MAX_MAINTENANCE_PROMPT_CHARS:
            raise BismuthError("maintenance metadata exceeds context budget")
        return [[]]
    packets: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for document in documents:
        candidate = [*current, document]
        if current and _prompt_chars(build(candidate)) > MAX_MAINTENANCE_PROMPT_CHARS:
            packets.append(current)
            current = [document]
        else:
            current = candidate
        if _prompt_chars(build(current)) > MAX_MAINTENANCE_PROMPT_CHARS:
            # A card is already a summary rather than original bytes. Pathological legacy
            # cards can still be larger than a whole request; retain the handle and prefix
            # instead of sending an over-context request that the provider must reject.
            document_id, description = current[0]
            empty_size = _prompt_chars(build([(document_id, "")]))
            allowance = MAX_MAINTENANCE_PROMPT_CHARS - empty_size - 64
            if allowance <= 0:
                raise BismuthError("boundary metadata alone exceeds maintenance context budget")
            current = [(document_id, description[:allowance])]
    if current:
        packets.append(current)
    return packets


def _value_packets(
    items: list[PacketT], build: Callable[[list[PacketT]], Prompt]
) -> list[list[PacketT]]:
    """Bound arbitrary compact metadata lists such as signs or boundary groups."""
    packets: list[list[PacketT]] = []
    current: list[PacketT] = []
    for item in items:
        candidate = [*current, item]
        if current and _prompt_chars(build(candidate)) > MAX_MAINTENANCE_PROMPT_CHARS:
            packets.append(current)
            current = [item]
        else:
            current = candidate
        if _prompt_chars(build(current)) > MAX_MAINTENANCE_PROMPT_CHARS:
            raise BismuthError("one maintenance sign exceeds context budget")
    if current:
        packets.append(current)
    return packets


def _relevant_children(
    documents: list[tuple[str, str]], children: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    names: set[str] = set()
    for _, description in documents:
        if not description.startswith("current="):
            continue
        current = description.removeprefix("current=").split(" | ", 1)[0]
        parts = PurePosixPath(current).parts
        if len(parts) > 1:
            names.add(parts[0])
    return [child for child in children if child[0] in names]


def _groups_for_ids(groups: list[prompts.Group], ids: set[str]) -> list[prompts.Group]:
    return [
        group.model_copy(
            update={
                "document_ids": [
                    document_id for document_id in group.document_ids if document_id in ids
                ]
            }
        )
        for group in groups
    ]


def _groups_relevant_to_ids(groups: list[prompts.Group], ids: set[str]) -> list[prompts.Group]:
    return [group for group in groups if ids.intersection(group.document_ids)]


def _sketch_packets(
    folder: PurePosixPath, sketches: list[prompts.ReplacementSketch]
) -> list[list[prompts.ReplacementSketch]]:
    packets: list[list[prompts.ReplacementSketch]] = []
    current: list[prompts.ReplacementSketch] = []
    for sketch in sketches:
        candidate = [*current, sketch]
        prompt = prompts.build_replacement_reduce(path=str(folder), sketches=candidate)
        if current and _prompt_chars(prompt) > MAX_MAINTENANCE_PROMPT_CHARS:
            packets.append(current)
            current = [sketch]
        else:
            current = candidate
    if current:
        packets.append(current)
    return packets


def _emerging_packets(
    *,
    folder: PurePosixPath,
    purpose: str,
    axis: str,
    children: list[tuple[str, str]],
    candidates: list[prompts.Emerging],
) -> list[list[prompts.Emerging]]:
    packets: list[list[prompts.Emerging]] = []
    current: list[prompts.Emerging] = []
    for candidate in candidates:
        proposed = [*current, candidate]
        prompt = prompts.build_emerging_reduce(
            path=str(folder),
            purpose=purpose,
            axis=axis,
            children=children,
            candidates=proposed,
        )
        if current and _prompt_chars(prompt) > MAX_MAINTENANCE_PROMPT_CHARS:
            packets.append(current)
            current = [candidate]
        else:
            current = proposed
    if current:
        packets.append(current)
    return packets


def _normalise_sketch(sketch: prompts.ReplacementSketch) -> prompts.ReplacementSketch:
    return sketch.model_copy(
        update={
            "signs": [
                sign.model_copy(update={"note": routing_purpose(sign.note, fallback=sign.name)})
                for sign in sketch.signs
            ]
        }
    )


def _normalise_replacement(replacement: prompts.Replacement) -> prompts.Replacement:
    return replacement.model_copy(
        update={
            "groups": [
                group.model_copy(update={"note": routing_purpose(group.note, fallback=group.name)})
                for group in replacement.groups
            ]
        }
    )


def _free_filename(filename: str, taken: set[str]) -> str:
    """Choose a case-insensitively unique name inside one replacement class."""
    if filename.casefold() not in taken:
        return filename
    stem, dot, extension = filename.rpartition(".")
    stem, extension = (stem, f".{extension}") if dot else (filename, "")
    index = 2
    while True:
        candidate = f"{stem} ({index}){extension}"
        if candidate.casefold() not in taken:
            return candidate
        index += 1


def _in_inbox(path: PurePosixPath) -> bool:
    return bool(path.parts) and path.parts[0] == INBOX.parts[0]


def _failed_boundary_checks(audit: prompts.BoundaryAudit) -> list[str]:
    return [
        name
        for name in (
            "one_property",
            "names_answer_question",
            "mutually_exclusive",
            "useful_for_navigation",
            "notes_are_routing_signs",
        )
        if not getattr(audit, name)
    ]


def _failed_routing_checks(audit: prompts.RoutingAudit) -> list[str]:
    return [
        name for name in ("assignments_match_signs", "no_forced_fit") if not getattr(audit, name)
    ]
