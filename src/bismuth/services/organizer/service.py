"""Autonomous librarian orchestration over scoped tools and validated plans."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath

from agentkit import Agent, AgentEvent, ChatModel, FunctionTool, RunResult, ToolCall
from agentkit.loop import OnEvent

from bismuth.domain.charter import CHARTER_FILENAME, Charter, boundary_purpose
from bismuth.domain.document import sidecar_name
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.logging_setup import log_context, log_trace
from bismuth.ports.catalog import Catalog
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.charters import CharterService
from bismuth.services.maintenance_windows import MAX_WINDOW_DOCUMENTS
from bismuth.services.organizer.planning import (
    ProposedBoundary,
    ReorgProposal,
    ReorgResult,
    _boundary_parent,
    _family_groups,
    _finding_signature,
    _folder,
    _plan_summary,
    _ReviewIssue,
    _ReviewOutcome,
    _stored_folder,
    _validate_shadow_plan,
    _within,
    build_submit_plan_tool,
)
from bismuth.services.organizer.prompts import (
    _LIBRARIAN_CONTEXT,
    DEFAULT_ORGANIZE_INSTRUCTION,
    SYSTEM_ASK,
    SYSTEM_BOUNDARY_EXPLORE,
    SYSTEM_CRITIC_CONCLUDE,
    SYSTEM_MEMBERSHIP_EXPLORE,
    SYSTEM_ORGANIZE_CONCLUDE,
    SYSTEM_ORGANIZE_EXPLORE,
)
from bismuth.services.organizer.tools import (
    _BoundaryPlan,
    _compact_card,
    _document_handles,
    _document_paths_by_id,
    _FinishExplorationArgs,
    _NoChangeArgs,
    _PlanMove,
    _SemanticReviewArgs,
    _SubmitPlanArgs,
    build_arrivals_tool,
    build_read_tools,
    family_handle_units,
)
from bismuth.services.transactor import Transactor


def _trace_agent_events(stage: str, downstream: OnEvent | None) -> OnEvent:
    """Persist orchestration decisions while preserving an optional UI/debug sink."""

    def _record(event: AgentEvent) -> None:
        log_trace(f"agent.{event.kind}", stage=stage, **event.data)
        if downstream is not None:
            downstream(event)

    return _record


def _flatten_tool_evidence(result: RunResult) -> str:
    """Project exact observations into a fresh transcript without old tool-call shapes."""

    names: dict[str, str] = {}
    observations: list[str] = []
    for message in result.messages:
        for call in message.tool_calls:
            names[call.id] = call.name
        if message.role != "tool":
            continue
        tool_name = names.get(message.tool_call_id or "", "observation")
        observations.append(
            f"<observation source={json.dumps(tool_name)}>\n{message.content}\n</observation>"
        )
    return "\n\n".join(observations) or "(the explorer collected no tool observations)"


def _finish_exploration_tool() -> FunctionTool:
    """Give read-only agents a real, schema-visible way to end evidence collection."""

    async def _finish(args: _FinishExplorationArgs) -> str:
        return "Exploration complete. Return no more tool calls. Summary: " + " ".join(
            args.summary.split()
        )

    return FunctionTool(
        name="finish_exploration",
        description=(
            "Finish the read-only evidence phase once sufficient evidence has been collected. "
            "This records no verdict and mutates nothing."
        ),
        params=_FinishExplorationArgs,
        handler=_finish,
        read_only=True,
        concurrency_safe=False,
    )


def _exploration_accepted(call: ToolCall, content: str, kind: str) -> bool:
    """A successful finish_exploration result is a hard phase boundary."""

    return (
        kind == "tool_result"
        and call.name == "finish_exploration"
        and content.startswith("Exploration complete.")
    )


class AgentService:
    """Runs Q&A plus shadow-planned, transactionally applied maintenance."""

    def __init__(
        self,
        *,
        model: ChatModel,
        vault: Vault,
        charters: CharterService,
        transactor: Transactor | None = None,
        catalog: Catalog | None = None,
    ) -> None:
        self._model = model
        self._vault = vault
        self._charters = charters
        self._transactor = transactor
        self._catalog = catalog

    def _evidence_handles(
        self,
        all_handles: dict[str, PurePosixPath],
        movable_handles: dict[str, PurePosixPath],
        *,
        scope: PurePosixPath,
        focused: bool,
    ) -> dict[str, PurePosixPath]:
        """Expose the current window plus prior committed shelves, never loose backlog.

        A planner may learn an established boundary from documents already below its
        direct children. Loose documents at the reviewed parent are unprocessed backlog
        unless they are in the current movable window, so neither planner nor critic may
        use them as counterexamples.
        """

        if not focused:
            return all_handles
        movable_paths = set(movable_handles.values())
        evidence = dict(movable_handles)
        reference_paths = [
            path
            for path in all_handles.values()
            if path not in movable_paths and _within(path, scope) and path.parent != scope
        ]
        # D/F are action capabilities. Existing committed documents are useful evidence,
        # but receive a visibly different namespace so a conclusion cannot mistake an
        # inventory card for permission to move it.
        evidence.update(
            {f"R{index:06d}": path for index, path in enumerate(reference_paths, start=1)}
        )
        return evidence

    def scope_fingerprint(self, scope: str) -> str:
        """Stable evidence identity used to avoid reviewing an unchanged scope twice."""

        parent = _boundary_parent(scope)
        if parent is None or not self._vault.is_dir(parent):
            return ""
        depth = len(parent.parts)
        folders = sorted(
            str(folder)
            for folder in self._vault.iter_folders()
            if len(folder.parts) == depth + 1 and folder.parts[:depth] == parent.parts
        )
        documents = sorted(str(path) for path in self._vault.iter_files(parent, recursive=True))
        try:
            try:
                charter = self._charters.load(parent)
            except Exception:
                charter = None
        except Exception:
            charter = None
        boundary = (
            {
                "basis": charter.split_basis,
                "question": charter.split_question,
                "managed": charter.managed,
            }
            if charter is not None
            else {}
        )
        raw = json.dumps(
            {
                "scope": _stored_folder(parent),
                "folders": folders,
                "documents": documents,
                "boundary": boundary,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    async def _review_candidate(
        self,
        boundaries: list[ProposedBoundary],
        *,
        handles: dict[str, PurePosixPath],
        on_event: OnEvent | None,
    ) -> _ReviewOutcome:
        """Have two isolated critics attack the exact validated candidate, fail closed."""

        prompt = self._candidate_evidence(boundaries, handles=handles)
        problems: list[str] = []
        issues: list[_ReviewIssue] = []

        def capture_review(
            sink: list[_SemanticReviewArgs],
        ) -> Callable[[_SemanticReviewArgs], Awaitable[str]]:
            async def _capture(args: _SemanticReviewArgs) -> str:
                sink[:] = [args]
                return "Semantic review recorded. End the review now."

            return _capture

        for stage, explore_system in (
            ("boundary_critic", SYSTEM_BOUNDARY_EXPLORE),
            ("membership_critic", SYSTEM_MEMBERSHIP_EXPLORE),
        ):
            verdicts: list[_SemanticReviewArgs] = []

            explorer = Agent(
                model=self._model,
                tools=[
                    *build_read_tools(
                        self._vault,
                        self._charters,
                        handles=handles,
                        restrict_documents=True,
                        catalog=self._catalog,
                    ),
                    _finish_exploration_tool(),
                ],
                system=explore_system,
                max_turns=64,
                conclusion_tools={"finish_exploration"},
                conclusion_accepted=_exploration_accepted,
                require_conclusion_tool=False,
                tool_choice="required",
                context_policy=_LIBRARIAN_CONTEXT,
                on_event=_trace_agent_events(f"{stage}.explore", on_event),
            )
            explore_run_id = f"agent_{uuid.uuid4().hex}"
            with log_context(agent_run_id=explore_run_id, stage=f"{stage}.explore"):
                exploration = await explorer.run(prompt)

            submit_review = FunctionTool(
                name="submit_review",
                description=(
                    "Submit this critic's only verdict for the exact candidate. Empty "
                    "findings means the candidate survived this attack."
                ),
                params=_SemanticReviewArgs,
                handler=capture_review(verdicts),
                read_only=True,
                concurrency_safe=False,
            )
            critic = Agent(
                model=self._model,
                tools=[submit_review],
                system=SYSTEM_CRITIC_CONCLUDE,
                max_turns=64,
                conclusion_tools={"submit_review"},
                conclusion_accepted=lambda call, content, kind: (
                    kind == "tool_result"
                    and call.name == "submit_review"
                    and content.startswith("Semantic review recorded.")
                ),
                context_policy=_LIBRARIAN_CONTEXT,
                tool_choice="required",
                on_event=_trace_agent_events(f"{stage}.conclusion", on_event),
            )
            critic_run_id = f"agent_{uuid.uuid4().hex}"
            conclusion_input = (
                f"EXACT CANDIDATE\n{prompt}\n\n"
                f"EXPLORER OBSERVATIONS\n{_flatten_tool_evidence(exploration)}"
            )
            with log_context(agent_run_id=critic_run_id, stage=f"{stage}.conclusion"):
                result = await critic.run(conclusion_input)
            if not verdicts:
                reason = f"{stage} stopped with {result.stopped} without submit_review"
                log_trace("agent.semantic_review_incomplete", stage=stage, reason=reason)
                problems.append(reason)
                continue
            verdict = verdicts[0]
            allowed_kinds = (
                {
                    "overlap",
                    "contains_sibling",
                    "level_mismatch",
                    "mixed_axis",
                    "catch_all",
                    "over_partition",
                    "duplicate_boundary",
                    "insufficient_evidence",
                }
                if stage == "boundary_critic"
                else {
                    "mixed_axis",
                    "family_split",
                    "forced_fit",
                    "insufficient_evidence",
                }
            )
            ignored = [finding for finding in verdict.findings if finding.kind not in allowed_kinds]
            if ignored:
                log_trace(
                    "agent.semantic_findings_ignored",
                    stage=stage,
                    findings=[finding.model_dump(mode="json") for finding in ignored],
                )
            family_partition = self._is_grounded_family_partition(boundaries)
            eligible = [
                finding
                for finding in verdict.findings
                if finding.blocking and finding.kind in allowed_kinds
            ]
            blocking = [
                finding
                for finding in eligible
                if all(handle in handles for handle in finding.evidence_handles)
                # Exact-title/subordinate families are already enforced by the
                # deterministic validator. A semantic critic may not redefine a
                # co-located family as a forced fit merely because its members have
                # different legal types or are different editions.
                and finding.kind != "family_split"
                and not self._duplicate_finding_only_names_existing_targets(
                    finding.subjects, boundaries
                )
                and not (
                    family_partition
                    and finding.kind
                    in {"level_mismatch", "mixed_axis", "over_partition", "forced_fit"}
                )
            ]
            if downgraded := [finding for finding in eligible if finding not in blocking]:
                log_trace(
                    "agent.semantic_findings_downgraded",
                    stage=stage,
                    family_partition=family_partition,
                    findings=[finding.model_dump(mode="json") for finding in downgraded],
                )
            log_trace(
                "agent.semantic_reviewed",
                stage=stage,
                summary=f"findings={len(verdict.findings)} blocking={len(blocking)}",
                findings=[finding.model_dump(mode="json") for finding in verdict.findings],
            )
            for finding in blocking:
                problem = (
                    f"{stage} {finding.kind}: "
                    f"{', '.join(finding.subjects) or '(boundary)'} — {finding.instruction}"
                )
                problems.append(problem)
                issues.append(
                    _ReviewIssue(
                        problem=problem,
                        kind=finding.kind,
                        evidence_handles=tuple(dict.fromkeys(finding.evidence_handles)),
                        candidate_signature=_finding_signature(
                            boundaries,
                            finding.evidence_handles,
                            handles=handles,
                            kind=finding.kind,
                        ),
                    )
                )
        return _ReviewOutcome(tuple(problems), tuple(issues))

    def _duplicate_finding_only_names_existing_targets(
        self,
        subjects: Sequence[str],
        boundaries: Sequence[ProposedBoundary],
    ) -> bool:
        """Reject a critic category error, not a semantic counterexample.

        ``add_sibling`` may route focused documents to an established sibling while
        creating a different sibling.  An existing destination cannot itself be a
        newly duplicated boundary, even though it appears in the submitted move list.
        """

        if not subjects:
            return False
        existing: set[str] = set()
        for boundary in boundaries:
            for move in boundary.moves:
                target = PurePosixPath(move.target)
                if self._vault.is_dir(target):
                    existing.update({str(target).strip("/").casefold(), target.name.casefold()})
        normalized = {subject.strip().strip("/").casefold() for subject in subjects}
        return bool(normalized) and normalized <= existing

    def _is_grounded_family_partition(self, boundaries: list[ProposedBoundary]) -> bool:
        """Whether every new sibling is exactly one durable named document family."""

        if not boundaries or any(
            boundary.operation != "create_boundary" for boundary in boundaries
        ):
            return False
        for boundary in boundaries:
            for move in boundary.moves:
                paths = {PurePosixPath(path) for path in move.paths}
                if len(paths) < 2 or paths not in _family_groups(self._vault, paths):
                    return False
        return True

    def _candidate_evidence(
        self,
        boundaries: list[ProposedBoundary],
        *,
        handles: dict[str, PurePosixPath],
    ) -> str:
        """Build neutral counterexample-oriented evidence; it never decides semantics."""

        handle_by_path = {path: handle for handle, path in handles.items()}
        payload: list[dict[str, object]] = []
        evidence: list[str] = []
        for boundary in boundaries:
            parent = _folder(boundary.parent)
            payload.append(
                {
                    "parent": boundary.parent or "/",
                    "operation": boundary.operation,
                    "axis": boundary.axis,
                    "axis_question": boundary.axis_question,
                    "moves": [
                        {
                            "target": move.target,
                            "target_state": (
                                "existing_target"
                                if self._vault.is_dir(PurePosixPath(move.target))
                                else "new_target"
                            ),
                            "document_ids": [
                                handle_by_path.get(PurePosixPath(path), "MISSING")
                                for path in move.paths
                            ],
                        }
                        for move in boundary.moves
                    ],
                }
            )
            charter = self._charters.load(parent)
            if charter is not None:
                evidence.append(
                    f"CURRENT {boundary.parent or '/'}: basis={charter.split_basis!r}; "
                    f"question={charter.split_question!r}"
                )
            depth = len(parent.parts)
            for child in sorted(self._vault.iter_folders(), key=str):
                if len(child.parts) != depth + 1 or child.parts[:depth] != parent.parts:
                    continue
                try:
                    note = self._charters.load(child)
                except Exception:
                    note = None
                evidence.append(
                    f"SIBLING {child}: "
                    f"{note.purpose if note is not None else '(no managed boundary note)'}"
                )
                # Representative cards are navigation evidence only. The critic can page
                # the complete inventory when a representative suggests a counterexample.
                sample = [
                    document
                    for document in sorted(self._vault.iter_files(child, recursive=True), key=str)
                    if document in handle_by_path
                ][:3]
                evidence.extend(
                    _compact_card(
                        self._vault,
                        document,
                        handle_by_path.get(document, ""),
                        catalog=self._catalog,
                    )
                    for document in sample
                )
            for move in boundary.moves:
                target_state = (
                    "EXISTING_TARGET"
                    if self._vault.is_dir(PurePosixPath(move.target))
                    else "NEW_TARGET"
                )
                evidence.append(f"PROPOSED {target_state} {move.target}")
                evidence.extend(
                    _compact_card(
                        self._vault,
                        PurePosixPath(path),
                        handle_by_path.get(PurePosixPath(path), ""),
                        catalog=self._catalog,
                    )
                    for path in move.paths
                )
        return (
            "Review this exact validated candidate. Inspect more evidence with tools when "
            "needed, then call submit_review.\n\nCANDIDATE\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n\nCOUNTEREXAMPLE STARTING EVIDENCE\n"
            + "\n".join(evidence)
        )

    async def ask(self, question: str, *, on_event: OnEvent | None = None) -> RunResult:
        event_sink = _trace_agent_events("ask", on_event)
        agent = Agent(
            model=self._model,
            tools=build_read_tools(self._vault, self._charters, catalog=self._catalog),
            system=SYSTEM_ASK,
            max_turns=64,
            conclusion_turns=8,
            context_policy=_LIBRARIAN_CONTEXT,
            on_event=event_sink,
        )
        return await agent.run(question)

    def loose_document_ids(self, scope: str = "") -> list[str]:
        """Stable IDs for documents directly on a boundary rather than inside a shelf."""
        scope_path = _boundary_parent(scope)
        if scope_path is None:
            return []
        paths = _document_paths_by_id(self._vault)
        return [
            document_id
            for document_id, path in sorted(paths.items(), key=lambda item: str(item[1]).casefold())
            if path.parent == scope_path
        ]

    def next_affected_scope(
        self,
        document_ids: Sequence[str],
        *,
        exclude: Collection[str] = (),
    ) -> tuple[str, tuple[str, ...]] | None:
        """Choose one non-root boundary for a context-isolated maintenance pass.

        This only schedules where the next librarian looks. Whether that folder should
        split, and along which axis, remains the model's decision. One scope per arrival
        window bounds latency while repeated windows carry the tree deeper over time.
        """

        paths = _document_paths_by_id(self._vault)
        excluded = set(exclude)
        folders = set(self._vault.iter_folders())

        def direct_children(parent: PurePosixPath) -> set[PurePosixPath]:
            depth = len(parent.parts)
            return {
                folder
                for folder in folders
                if len(folder.parts) == depth + 1 and folder.parts[:depth] == parent.parts
            }

        def has_established_boundary(parent: PurePosixPath) -> bool:
            if len(direct_children(parent)) < 2:
                return False
            try:
                charter = self._charters.load(parent)
            except Exception:
                return False
            return bool(charter is not None and charter.split_basis and charter.split_question)

        candidates: set[PurePosixPath] = set()
        for document_id in document_ids:
            path = paths.get(document_id)
            if path is None or not path.parent.parts:
                continue
            current = path.parent
            # A flat shelf needs one full arrival window of durable evidence before it
            # is subdivided. The previous four-document trigger created brittle type
            # shelves from whichever tiny sample happened to arrive first.
            if (
                not direct_children(current)
                and self._vault.count_files(current, recursive=False) >= MAX_WINDOW_DOCUMENTS
            ):
                candidates.add(current)
            # If placement put this arrival below an established boundary value, review
            # that boundary parent. Reviewing the leaf itself cannot move a misfiled
            # document to a sibling and creates an impossible scope contract.
            for depth in range(len(current.parts) - 1, 0, -1):
                ancestor = PurePosixPath(*current.parts[:depth])
                children = direct_children(ancestor)
                if has_established_boundary(ancestor) and any(
                    _within(path, child) for child in children
                ):
                    candidates.add(ancestor)
                    break
        feasible = [
            (
                parent,
                [
                    document_id
                    for document_id in document_ids
                    if (path := paths.get(document_id)) is not None and _within(path, parent)
                ],
            )
            for parent in candidates
            if str(parent) not in excluded
        ]
        if not feasible:
            return None
        parent, ids = min(
            feasible,
            key=lambda item: (
                len(item[0].parts),
                -self._vault.count_files(item[0], recursive=False),
                str(item[0]).casefold(),
            ),
        )
        return str(parent), tuple(ids)

    async def propose_reorg(
        self,
        instruction: str = DEFAULT_ORGANIZE_INSTRUCTION,
        *,
        scope: str = "",
        focus_document_ids: Sequence[str] = (),
        on_event: OnEvent | None = None,
    ) -> ReorgProposal:
        """Inspect the vault and return one validated shadow plan. Never mutates."""
        scope_path = _boundary_parent(scope)
        if scope_path is None:
            return ReorgProposal([], [], f"Invalid scope: {scope}", [], ["invalid scope"])
        if not self._vault.is_dir(scope_path):
            return ReorgProposal([], [], f"No such scope: {scope or '/'}", [], ["missing scope"])
        all_handles = _document_handles(self._vault)
        if focus_document_ids:
            path_by_id = _document_paths_by_id(self._vault)
            focus_paths = {
                path_by_id[document_id]
                for document_id in focus_document_ids
                if document_id in path_by_id
            }
            handles = {handle: path for handle, path in all_handles.items() if path in focus_paths}
        else:
            handles = all_handles
        evidence_handles = self._evidence_handles(
            all_handles,
            handles,
            scope=scope_path,
            focused=bool(focus_document_ids),
        )
        boundaries: list[ProposedBoundary] = []
        problems: list[str] = []
        no_change_reason: list[str] = []

        async def _finish_no_change(args: _NoChangeArgs) -> str:
            # A rejected draft is not the final decision. The model may inspect the
            # rejection and explicitly conclude that preserving the tree is safer.
            boundaries.clear()
            problems.clear()
            no_change_reason[:] = [" ".join(args.reason.split())]
            return "No-change decision recorded. End the run now."

        family_units = family_handle_units(
            self._vault,
            handles=handles,
            document_ids=focus_document_ids,
            catalog=self._catalog,
        )
        family_members = {member for members in family_units.values() for member in members}
        submittable_units = [
            *[handle for handle in handles if handle not in family_members],
            *family_units,
        ]
        exploration_tools = [
            *build_read_tools(
                self._vault,
                self._charters,
                handles=evidence_handles,
                restrict_documents=bool(focus_document_ids),
                catalog=self._catalog,
            ),
            build_arrivals_tool(
                self._vault,
                handles=handles,
                document_ids=focus_document_ids,
                catalog=self._catalog,
                family_units=family_units,
            ),
            _finish_exploration_tool(),
        ]
        conclusion_tools = [
            build_submit_plan_tool(
                self._vault,
                scope=scope_path,
                handles=handles,
                sink=boundaries,
                problem_sink=problems,
                family_units=family_units,
                bounded=bool(focus_document_ids),
                semantic_reviewer=lambda candidate: self._review_candidate(
                    candidate,
                    handles=evidence_handles,
                    on_event=on_event,
                ),
            ),
            FunctionTool(
                name="finish_no_change",
                description=(
                    "Explicitly finish with no structure change after complete inspection. "
                    "Use only when no coherent improvement survives verification; do not "
                    "use it when loose arrivals can be moved into existing folders."
                ),
                params=_NoChangeArgs,
                handler=_finish_no_change,
                read_only=True,
                concurrency_safe=False,
            ),
        ]
        explorer = Agent(
            model=self._model,
            tools=exploration_tools,
            system=SYSTEM_ORGANIZE_EXPLORE,
            max_turns=64,
            conclusion_tools={"finish_exploration"},
            conclusion_accepted=_exploration_accepted,
            require_conclusion_tool=False,
            tool_choice="required",
            context_policy=_LIBRARIAN_CONTEXT,
            on_event=_trace_agent_events("planner.explore", on_event),
        )
        concluder = Agent(
            model=self._model,
            tools=conclusion_tools,
            system=SYSTEM_ORGANIZE_CONCLUDE,
            max_turns=64,
            conclusion_tools={"submit_plan", "finish_no_change"},
            conclusion_accepted=lambda call, content, kind: (
                kind == "tool_result"
                and (
                    call.name == "finish_no_change"
                    or (call.name == "submit_plan" and content.startswith("Shadow plan accepted:"))
                )
            ),
            context_policy=_LIBRARIAN_CONTEXT,
            tool_choice="required",
            on_event=_trace_agent_events("planner.conclusion", on_event),
        )
        focused_instruction = (
            f"{instruction}\n\nThis pass was triggered by a bounded window of "
            f"{len(focus_document_ids)} addressable documents, including any exact family "
            "closure required from an earlier window. Inspect them with arrivals; "
            "these are the only documents you can read or move in this pass. Treat the existing "
            "tree and folder notes as prior memory; documents outside this window are not "
            "addressable and must remain untouched. "
            f"Your assigned scope is {scope or '/'} and every submitted boundary parent must "
            "equal that scope. "
            + (
                "You are the global architect: maintain only the root sibling boundary."
                if not scope_path.parts
                else "You are a local organizer: never create or modify a boundary outside this scope."
            )
        )
        exploration_instruction = (
            "Inspect the bounded scope only and collect the minimum evidence needed by "
            "the separate conclusion phase. Do not propose, enumerate, or narrate a "
            "folder plan. Call tree and arrivals, make only genuinely necessary bounded "
            "reads, then finish_exploration.\n\n"
            f"Scope: {scope or '/'}\n"
            f"Addressable document units: {','.join(submittable_units)}"
        )
        explorer_run_id = f"agent_{uuid.uuid4().hex}"
        with log_context(agent_run_id=explorer_run_id, stage="planner.explore"):
            exploration = await explorer.run(exploration_instruction)
        conclusion_input = (
            f"TASK AND SCOPE\n{focused_instruction}\n\n"
            "ACTION CAPABILITIES\n"
            "Only the following D/F units may appear in submit_plan; every R handle in "
            "observations is reference-only:\n" + ",".join(submittable_units) + "\n\n"
            f"EXPLORER OBSERVATIONS\n{_flatten_tool_evidence(exploration)}"
        )
        planner_run_id = f"agent_{uuid.uuid4().hex}"
        with log_context(agent_run_id=planner_run_id, stage="planner.conclusion"):
            result = await concluder.run(conclusion_input)
        if not boundaries and not no_change_reason:
            if problems:
                # A rejected candidate is a safe no-change outcome, not a failed ingest.
                # Preserve the concrete first reason for the checkpoint and let later
                # bounded windows continue instead of permanently blocking maintenance.
                reason = "No structure candidate survived validation; current tree preserved: "
                no_change_reason[:] = [reason + problems[0]]
                log_trace(
                    "agent.plan_abandoned",
                    scope=scope or "/",
                    problems=problems,
                )
                problems.clear()
            else:
                problems.append(
                    "planner reached its final safety guard before submitting a plan"
                    if result.stopped in {"max_turns", "stalled"}
                    else "planner ended without submit_plan or finish_no_change"
                )
        moves = [move for boundary in boundaries for move in boundary.moves]
        return ReorgProposal(
            moves=moves,
            renames=[],
            summary=(
                no_change_reason[0]
                if no_change_reason
                else _plan_summary(boundaries)
                if boundaries
                else result.text
            ),
            boundaries=boundaries,
            problems=problems,
        )

    async def reorganize(
        self,
        instruction: str = DEFAULT_ORGANIZE_INSTRUCTION,
        *,
        scope: str = "",
        focus_document_ids: Sequence[str] = (),
        on_event: OnEvent | None = None,
    ) -> ReorgResult:
        """Plan against a snapshot and atomically apply only a still-valid plan."""
        scope_path = _boundary_parent(scope)
        if scope_path is None or not self._vault.is_dir(scope_path):
            proposal = ReorgProposal(
                moves=[],
                renames=[],
                summary=f"Invalid or missing scope: {scope}",
                boundaries=[],
                problems=["invalid scope"],
            )
            return ReorgResult(proposal=proposal, applied=False)
        proposal = await self.propose_reorg(
            instruction,
            scope=scope,
            focus_document_ids=focus_document_ids,
            on_event=on_event,
        )
        if not proposal.boundaries:
            unresolved = self._unresolved_at_scope_root(focus_document_ids, scope_path)
            log_trace(
                "agent_maintenance.skipped",
                scope=scope or "/",
                problems=proposal.problems,
                summary=proposal.summary,
            )
            return ReorgResult(
                proposal=proposal,
                applied=False,
                unresolved_document_ids=unresolved,
            )

        actionable_boundaries = [
            boundary for boundary in proposal.boundaries if any(move.paths for move in boundary.moves)
        ]
        if not actionable_boundaries:
            unresolved = self._unresolved_at_scope_root(focus_document_ids, scope_path)
            return ReorgResult(
                proposal=ReorgProposal(
                    moves=[],
                    renames=[],
                    summary=proposal.summary,
                    boundaries=[],
                    problems=proposal.problems,
                ),
                applied=False,
                unresolved_document_ids=unresolved,
            )

        # Rebuild the typed submission and validate again immediately before execution.
        # The first validation happened during the tool loop; this closes the window in
        # which another filesystem actor could invalidate the snapshot.
        current_handles = _document_handles(self._vault)
        handle_by_path = {path: handle for handle, path in current_handles.items()}
        proposal_paths = {
            PurePosixPath(path)
            for boundary in actionable_boundaries
            for move in boundary.moves
            for path in move.paths
        }
        validation_handles = {
            handle: path for handle, path in current_handles.items() if path in proposal_paths
        }
        submitted = _SubmitPlanArgs(
            boundaries=[
                _BoundaryPlan(
                    parent=boundary.parent,
                    operation=boundary.operation,
                    axis=boundary.axis,
                    axis_question=boundary.axis_question,
                    moves=[
                        _PlanMove(
                            document_ids=[
                                handle_by_path.get(PurePosixPath(path), "MISSING")
                                for path in move.paths
                            ],
                            target=move.target,
                        )
                        for move in boundary.moves
                    ],
                )
                for boundary in actionable_boundaries
            ]
        )
        boundaries, problems = _validate_shadow_plan(
            self._vault,
            submitted,
            scope=scope_path,
            handles=validation_handles,
        )
        if problems:
            rejected = ReorgProposal(
                moves=[],
                renames=[],
                summary=proposal.summary,
                boundaries=[],
                problems=problems,
            )
            log_trace(
                "agent_maintenance.rejected",
                scope=scope or "/",
                problems=problems,
            )
            return ReorgResult(proposal=rejected, applied=False)

        moved = self._apply_boundaries(boundaries)
        unresolved = self._unresolved_at_scope_root(focus_document_ids, scope_path)
        applied = moved > 0
        applied_summary = _plan_summary(
            boundaries,
            moved=moved,
            unresolved_document_ids=unresolved,
        )
        proposal = ReorgProposal(
            moves=proposal.moves,
            renames=proposal.renames,
            summary=applied_summary,
            boundaries=proposal.boundaries,
            problems=proposal.problems,
        )
        log_trace(
            "agent_maintenance.applied" if applied else "agent_maintenance.skipped",
            scope=scope or "/",
            boundaries=len(boundaries),
            moved=moved,
            summary=applied_summary,
        )
        return ReorgResult(
            proposal=proposal,
            applied=applied,
            moved=moved,
            unresolved_document_ids=unresolved,
        )

    def _unresolved_at_scope_root(
        self,
        document_ids: Sequence[str],
        scope: PurePosixPath,
    ) -> tuple[str, ...]:
        """Return focused documents still loose at the reviewed boundary root."""
        paths = _document_paths_by_id(self._vault)
        return tuple(
            document_id
            for document_id in document_ids
            if (path := paths.get(document_id)) is not None and path.parent == scope
        )

    def _apply_boundaries(self, boundaries: list[ProposedBoundary]) -> int:
        """Compile an accepted shadow plan into one journal transaction."""
        if self._transactor is None:
            raise RuntimeError("autonomous maintenance requires a transactor")
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        made: set[PurePosixPath] = set()
        charter_updates: dict[PurePosixPath, Charter] = {}
        moved_from: set[PurePosixPath] = set()
        moved_to: set[PurePosixPath] = set()
        moved = 0

        def current_charter(path: PurePosixPath) -> Charter | None:
            return charter_updates.get(path) or self._charters.load(path)

        for boundary in boundaries:
            parent = _folder(boundary.parent)
            subtree_count = self._vault.count_files(parent, recursive=True)
            changes_boundary = boundary.operation in {
                "create_boundary",
                "replace_boundary",
            }
            if changes_boundary and self._charters.is_managed(parent):
                current = current_charter(parent)
                if current is None:
                    parent_charter = Charter(
                        path=parent,
                        title=parent.name if parent.parts else "/",
                        purpose=(
                            boundary_purpose(boundary.axis, parent.name) if parent.parts else ""
                        ),
                        managed=True,
                        split_basis=boundary.axis,
                        split_question=boundary.axis_question,
                        split_at_documents=subtree_count,
                    )
                else:
                    parent_charter = current.model_copy(
                        update={
                            "split_basis": boundary.axis,
                            "split_question": boundary.axis_question,
                            "split_at_documents": subtree_count,
                            "boundary_review_required": False,
                            "repair_pending": False,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                charter_updates[parent] = parent_charter

            for move in boundary.moves:
                target = PurePosixPath(move.target)
                if not self._vault.exists(target) and target not in made:
                    operations.append(Operation(kind=OperationKind.MKDIR, target=target))
                    made.add(target)
                writes_child_boundary = boundary.operation not in {
                    "route_existing",
                    "rehome_existing",
                }
                if writes_child_boundary and self._charters.is_managed(target):
                    current = current_charter(target)
                    if current is None:
                        target_charter = Charter(
                            path=target,
                            title=target.name,
                            purpose=boundary_purpose(
                                boundary.axis,
                                target.name,
                                question=boundary.axis_question,
                            ),
                            boundary_basis=boundary.axis,
                            boundary_question=boundary.axis_question,
                            boundary_answer=target.name,
                            managed=True,
                        )
                    else:
                        target_charter = current.model_copy(
                            update={
                                "purpose": boundary_purpose(
                                    boundary.axis,
                                    target.name,
                                    question=boundary.axis_question,
                                ),
                                "boundary_basis": boundary.axis,
                                "boundary_question": boundary.axis_question,
                                "boundary_answer": target.name,
                                "updated_at": datetime.now(UTC),
                            }
                        )
                    charter_updates[target] = target_charter
                for raw_source in move.paths:
                    source = PurePosixPath(raw_source)
                    if source.parent == target:
                        continue
                    destination = target / source.name
                    moved_from.add(source)
                    moved_to.add(destination)
                    operations.append(
                        Operation(
                            kind=OperationKind.MOVE,
                            source=source,
                            target=destination,
                            note=f"agent boundary: {boundary.axis}",
                        )
                    )
                    sidecar = source.parent / sidecar_name(source.name)
                    if self._vault.exists(sidecar):
                        operations.append(
                            Operation(
                                kind=OperationKind.MOVE,
                                source=sidecar,
                                target=target / sidecar_name(destination.name),
                                note="document sidecar",
                            )
                        )
                    moved += 1

            if changes_boundary:
                # A changed boundary contract belongs to every surviving sibling,
                # including shelves that received no document in this window.
                depth = len(parent.parts)
                for child in self._vault.iter_folders():
                    if len(child.parts) != depth + 1 or child.parts[:depth] != parent.parts:
                        continue
                    if not self._charters.is_managed(child):
                        continue
                    current = current_charter(child)
                    if current is None:
                        continue
                    charter_updates[child] = current.model_copy(
                        update={
                            "purpose": boundary_purpose(
                                boundary.axis,
                                child.name,
                                question=boundary.axis_question,
                            ),
                            "boundary_basis": boundary.axis,
                            "boundary_question": boundary.axis_question,
                            "boundary_answer": child.name,
                            "updated_at": datetime.now(UTC),
                        }
                    )

        if not moved:
            return 0
        for path in sorted(charter_updates, key=lambda item: (len(item.parts), str(item))):
            note_op, note_payload = self._charters.write_operation(charter_updates[path])
            operations.append(note_op)
            payloads[note_op.target] = note_payload
        operations.extend(self._retirement_operations(boundaries, moved_from, moved_to))
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"agent maintenance: {moved} document(s)",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        return moved

    def _retirement_operations(
        self,
        boundaries: list[ProposedBoundary],
        moved_from: set[PurePosixPath],
        moved_to: set[PurePosixPath],
    ) -> list[Operation]:
        """Retire only managed folders proven empty in the simulated final tree."""
        final_documents = set(self._vault.iter_files(PurePosixPath(), recursive=True))
        final_documents.difference_update(moved_from)
        final_documents.update(moved_to)
        parents = {
            _folder(boundary.parent)
            for boundary in boundaries
            if boundary.operation == "replace_boundary"
        }
        if not parents:
            return []
        folders = list(self._vault.iter_folders())
        candidates: set[PurePosixPath] = set()
        for folder in folders:
            if not folder.parts or folder.parts[0] == INBOX.parts[0]:
                continue
            if not any(_within(folder, parent) for parent in parents):
                continue
            if folder in parents or any(_within(document, folder) for document in final_documents):
                continue
            charter = self._charters.load(folder)
            if charter is None or not charter.managed:
                continue
            candidates.add(folder)

        # An RMDIR is intentionally non-recursive.  Preserve an ancestor's note too
        # when any descendant is human-owned or otherwise not eligible for retirement.
        retire = [
            folder
            for folder in candidates
            if all(
                other in candidates
                for other in folders
                if other != folder and _within(other, folder)
            )
        ]

        operations: list[Operation] = []
        for folder in sorted(retire, key=lambda item: len(item.parts), reverse=True):
            note = folder / CHARTER_FILENAME
            if self._vault.exists(note):
                operations.append(
                    Operation(kind=OperationKind.REMOVE, target=note, note="retire empty sign")
                )
            operations.append(
                Operation(kind=OperationKind.RMDIR, target=folder, note="retire empty shelf")
            )
        return operations
