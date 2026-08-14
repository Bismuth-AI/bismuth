"""Bounded model-evaluation passes for the structured local-growth harness."""

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


class SubdivisionEvaluationMixin:

    async def _existing_assignments(
        self,
        *,
        folder: PurePosixPath,
        contents: _Contents,
        charter: Charter,
    ) -> prompts.ExistingAssignments:
        merged: dict[str, list[str]] = {}
        handles = [f"F{index:03d}" for index in range(1, len(contents.children) + 1)]

        def build(packet: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
            return prompts.build_existing_assignments(
                path=str(folder),
                documents=packet,
                axis=charter.split_basis,
                axis_question=charter.split_question,
                children=contents.children,
            )

        for packet in _document_packets(contents.lines, build, max_documents=12):
            allowed = {document_id for document_id, _ in packet}
            answer = await self._llm.structured(build(packet), schema=prompts.ExistingAssignments)
            for group in answer.groups:
                if group.folder_id not in handles:
                    continue
                selected = [item for item in group.document_ids if item in allowed]
                merged.setdefault(group.folder_id, []).extend(selected)
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
        axis_question: str,
        spent: list[str],
        recently_rejected: list[str] | None = None,
    ) -> prompts.Emerging:
        def build(packet: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
            return prompts.build_emerging(
                path=str(folder),
                purpose=purpose,
                documents=packet,
                children=children,
                axis=axis,
                axis_question=axis_question,
                spent=spent,
                recently_rejected=recently_rejected,
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
                                axis_question=axis_question,
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

    async def _propose_initial_boundary(
        self,
        *,
        folder: PurePosixPath,
        purpose: str,
        contents: _Contents,
        documents: list[tuple[str, str]],
        spent: list[str],
    ) -> prompts.Division | None:
        """Establish a new axis only when two sibling classes evidence it together."""

        def build(packet: list[tuple[str, str]]) -> Prompt:
            return prompts.build_initial_boundary_sketch(
                path=str(folder), purpose=purpose, documents=packet, spent=spent
            )

        sketches: list[prompts.InitialBoundarySketch] = []
        for packet in _document_packets(documents, build, max_documents=12):
            sketch = await self._llm.structured(
                build(packet), schema=prompts.InitialBoundarySketch
            )
            if self._sketch_uses_card_metadata_facet(contents, sketch):
                log_trace(
                    "subdivide.rejected_sketch",
                    folder=str(folder),
                    reason="candidate signs reproduce a card metadata facet",
                    axis=sketch.basis,
                    signs=[sign.name for sign in sketch.signs],
                )
                continue
            sketches.append(sketch)
        while len(sketches) > 1:
            batches = _sketch_packets(folder, sketches)
            if all(len(batch) == 1 for batch in batches):
                return None
            reduced: list[prompts.InitialBoundarySketch] = []
            for batch in batches:
                if len(batch) == 1:
                    reduced.extend(batch)
                    continue
                result = await self._llm.structured(
                    prompts.build_replacement_reduce(
                        path=str(folder), sketches=batch, initial=True
                    ),
                    schema=prompts.InitialBoundarySketch,
                )
                if self._sketch_uses_card_metadata_facet(contents, result):
                    log_trace(
                        "subdivide.rejected_sketch",
                        folder=str(folder),
                        reason="reduced signs reproduce a card metadata facet",
                        axis=result.basis,
                        signs=[sign.name for sign in result.signs],
                    )
                    continue
                reduced.append(result)
            sketches = reduced
        if not sketches:
            return None

        sketch = sketches[0]
        assignments_by_sign: list[list[str]] = [[] for _ in sketch.signs]
        handles = [f"G{index:03d}" for index in range(1, len(sketch.signs) + 1)]

        def build_assignments(packet: list[tuple[str, str]]) -> Prompt:
            return prompts.build_replacement_assignments(
                path=str(folder), documents=packet, sketch=sketch
            )

        for packet in _document_packets(documents, build_assignments, max_documents=12):
            allowed = {document_id for document_id, _ in packet}
            answer = await self._llm.structured(
                build_assignments(packet), schema=prompts.ReplacementAssignments
            )
            seen: set[str] = set()
            for assignment in answer.groups:
                if assignment.folder_id not in handles:
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="initial assignment returned an unknown sign",
                    )
                    return None
                selected = [item for item in assignment.document_ids if item in allowed]
                if any(item in seen for item in selected):
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="initial assignment put one document in multiple classes",
                    )
                    return None
                seen.update(selected)
                assignments_by_sign[int(assignment.folder_id[1:]) - 1].extend(selected)

        groups = [
            prompts.Group(
                name=sign.name,
                note=boundary_purpose(sketch.basis, sign.name),
                document_ids=list(dict.fromkeys(assignments_by_sign[index])),
            )
            for index, sign in enumerate(sketch.signs)
            if len(set(assignments_by_sign[index])) >= 2
        ]
        if len(groups) < 2:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="first boundary requires at least two evidenced sibling classes",
                proposed=[group.name for group in groups],
            )
            return None
        return prompts.Division(
            basis=sketch.basis,
            basis_question=sketch.basis_question,
            groups=groups,
        )

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
        """Collect membership through bounded native-schema packets.

        The folder decision is a set-valued claim and the harness validates that exact
        set before moving anything. Keeping it structured avoids dozens of independent
        binary calls and preserves request-local D handles end to end.
        """

        def build(packet: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
            return prompts.build_members(
                path=str(folder),
                purpose=purpose,
                documents=packet,
                children=children,
                name=name,
                note=note,
            )

        claimed: list[str] = []
        for packet in _document_packets(documents, build, max_documents=12):
            allowed = {document_id for document_id, _ in packet}
            answer = await self._llm.structured(build(packet), schema=prompts.Members)
            claimed.extend(document_id for document_id in answer.document_ids if document_id in allowed)
        return prompts.Members(document_ids=list(dict.fromkeys(claimed)))


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

    async def _attempt_boundary_replacement(
        self,
        *,
        folder: PurePosixPath,
        purpose: str,
        charter: Charter,
        total: int,
        documents: list[tuple[str, str]],
        current_groups: list[prompts.Group],
        observed_failures: list[str],
        children: list[tuple[str, str]],
    ) -> prompts.Division | None:
        """Return a fully validated replacement, without controlling later filing."""
        replacement = await self._propose_replacement(
            folder=folder,
            purpose=purpose,
            charter=charter,
            total=total,
            documents=documents,
            children=children,
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
            available_document_ids=frozenset(document_id for document_id, _ in documents),
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
            documents=documents,
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
            documents=documents,
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
        """Build a complete replacement from bounded, independently validated stages.

        A complete ``Replacement`` reply grows with every document because it has to
        echo every handle.  Even a small input can therefore hit an output-token limit.
        Always design membership-free signs first, then assign short request-local
        handles in bounded packets and merge them here.  The same contract now covers
        small and large subtrees, so crossing a size threshold cannot change safety.
        """

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

        assignments_by_sign: list[list[str]] = [[] for _ in sketch.signs]
        handles = [f"G{index:03d}" for index in range(1, len(sketch.signs) + 1)]

        def build_assignments(packet: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
            return prompts.build_replacement_assignments(
                path=str(folder), documents=packet, sketch=sketch
            )

        for packet in _document_packets(documents, build_assignments, max_documents=12):
            allowed = {document_id for document_id, _ in packet}
            answer = await self._llm.structured(
                build_assignments(packet), schema=prompts.ReplacementAssignments
            )
            if answer.unassigned_document_ids:
                log_trace(
                    "subdivide.rejected",
                    folder=str(folder),
                    reason="replacement left documents unassigned",
                    replacement=True,
                )
                return None
            seen: set[str] = set()
            for group in answer.groups:
                if group.folder_id not in handles:
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="replacement assignment returned an unknown sign",
                        replacement=True,
                    )
                    return None
                selected = [item for item in group.document_ids if item in allowed]
                if any(item in seen for item in selected):
                    return None
                seen.update(selected)
                assignments_by_sign[int(group.folder_id[1:]) - 1].extend(selected)
            if seen != allowed:
                log_trace(
                    "subdivide.rejected",
                    folder=str(folder),
                    reason="replacement assignment did not cover its evidence packet",
                    replacement=True,
                )
                return None

        return prompts.Replacement(
            basis=sketch.basis,
            basis_question=sketch.basis_question,
            groups=[
                prompts.Group(
                    name=sign.name,
                    note=boundary_purpose(sketch.basis, sign.name),
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
            members_match_signs=all(check.members_match_signs for check in checks),
            no_remainder_sign=all(check.no_remainder_sign for check in checks),
            violations=[violation for check in checks for violation in check.violations],
        )

    async def _audit_class(
        self,
        *,
        folder: PurePosixPath,
        documents: list[tuple[str, str]],
        axis: str,
        axis_question: str,
        group: prompts.Group,
        children: list[tuple[str, str]],
    ) -> prompts.ClassAudit:
        """Audit a unary additive class without applying complete-boundary rules."""
        claimed = set(group.document_ids)
        members = [item for item in documents if item[0] in claimed]
        contrast = [item for item in documents if item[0] not in claimed][:8]

        def build(packet: list[tuple[str, str]]) -> Prompt:
            return prompts.build_class_audit(
                path=str(folder),
                axis=axis,
                axis_question=axis_question,
                name=group.name,
                total_loose_documents=len(documents),
                total_claimed_members=len(members),
                members=packet,
                contrast=contrast,
                siblings=children,
            )

        checks = [
            await self._llm.structured(build(packet), schema=prompts.ClassAudit)
            for packet in _document_packets(members, build, max_documents=12)
        ]

        async def audit_member(member: tuple[str, str]) -> tuple[str, str]:
            verdict = await self._llm.choose(
                prompts.build_member_fit_audit(
                    path=str(folder),
                    axis=axis,
                    axis_question=axis_question,
                    name=group.name,
                    document=member,
                ),
                choices=("BELONG", "STAY"),
                max_tokens=8,
                temperature=0.0,
            )
            return member[0], verdict

        # The aggregate critic checks whether the shelf itself is useful.  Each claimed
        # member is then checked independently so a false positive cannot hide in a long
        # set-valued response.  Calls are bounded by the shared local-model semaphore.
        member_verdicts = await _bounded_gather(members, audit_member)
        isolated_invalid = [
            document_id
            for document_id, verdict in member_verdicts
            if verdict != "BELONG"
        ]
        aggregate_invalid = [
            document_id
            for check in checks
            for document_id in check.invalid_member_ids
        ]
        isolated_invalid_set = set(isolated_invalid)
        aggregate_invalid_set = set(aggregate_invalid)
        disputed_members = [
            member
            for member in members
            if member[0] in aggregate_invalid_set and member[0] not in isolated_invalid_set
        ]

        async def audit_dispute(member: tuple[str, str]) -> tuple[str, str]:
            verdict = await self._llm.choose(
                prompts.build_member_dispute_audit(
                    path=str(folder),
                    axis=axis,
                    axis_question=axis_question,
                    name=group.name,
                    document=member,
                ),
                choices=("BELONG", "STAY"),
                max_tokens=8,
                temperature=0.0,
            )
            return member[0], verdict

        dispute_verdicts = await _bounded_gather(disputed_members, audit_dispute)
        dispute_invalid = [
            document_id
            for document_id, verdict in dispute_verdicts
            if verdict != "BELONG"
        ]
        log_trace(
            "subdivide.member_fit_audit",
            folder=str(folder),
            name=group.name,
            checked=len(member_verdicts),
            rejected=isolated_invalid,
            aggregate_suggestions=aggregate_invalid,
            disputes=dict(dispute_verdicts),
            dispute_rejected=dispute_invalid,
        )
        allowed = {document_id for document_id, _ in members}
        return prompts.ClassAudit(
            name_answers_question=all(check.name_answers_question for check in checks),
            recurring_class=all(check.recurring_class for check in checks),
            useful_for_navigation=all(check.useful_for_navigation for check in checks),
            distinct_from_contrast=all(check.distinct_from_contrast for check in checks),
            invalid_member_ids=list(
                dict.fromkeys(
                    document_id
                    for document_id in [*isolated_invalid, *dispute_invalid]
                    if document_id in allowed
                )
            ),
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
