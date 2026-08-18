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

import asyncio
import logging
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TypeVar

from bismuth.domain.charter import (
    CHARTER_FILENAME,
    Charter,
    routing_purpose,
    routing_sign,
    sign_refusal,
)
from bismuth.domain.document import DocumentCard, sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.maintenance import (
    ProposedClass,
    normalise_label,
    validate_grouping,
    validate_names,
    validate_plan,
    validate_split,
)
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_context, log_trace
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
    routed: bool = False
    """True when documents were moved into folders that already existed, rather than
    into a class created here. Those folders just gained evidence they did not have."""

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
    languages: list[str] = field(default_factory=list)
    """The language each card reported, so a prompt can name it back."""

    subjects: list[tuple[str, str]] = field(default_factory=list)
    """The same documents without their doc_type, for the one question that must not be
    answered with it. Grouping by the kind of instrument a document is fills a tree
    neatly and separates nothing a reader needs, and the old prompt argued against it in
    prose while the column sat in the evidence: gpt-5-nano grouped the three 시행규칙 out
    of seven documents whose subjects were unrelated."""

    @property
    def lines(self) -> list[tuple[str, str]]:
        return [(document_id, line) for document_id, line, _ in self.documents]

    @property
    def subject_lines(self) -> list[tuple[str, str]]:
        return list(self.subjects)

    @property
    def language(self) -> str:
        """The language to answer in, when the collection agrees on one.

        Read off the cards rather than assumed. An English instruction produces English
        folder names over a Korean archive unless the prompt says otherwise -- observed
        as twelve rejected proposals in one round, all of them named in English. Naming
        the collection's own language back to the model is evidence, not a builtin.
        """
        if not self.languages:
            return ""
        code, count = Counter(self.languages).most_common(1)[0]
        return code if count / len(self.languages) >= 0.75 else ""

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

        # A folder that just received documents is a folder where a class may now have
        # gathered, and routing is how most documents reach a deep folder: placement puts
        # them in the parent and an existing sign takes them. Nothing asked those folders
        # anything. One grew to 92 of 120 documents while being asked twice.
        #
        # Only folders that already existed. Recursing into a class created a moment ago
        # re-judges the same evidence, and once built 철학/현상학/체화된 인지 in a single
        # ingest, one document per level.
        for routed in [result for result in results if result.routed and result.created]:
            for target in routed.created:
                results.extend(
                    await self.consider(target, filename=filename, on_progress=on_progress)
                )

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

        # Before asking what could come out of this folder, ask whether the folder should
        # be here at all. It runs first and unconditionally, because the folders that most
        # need the question are the ones that have stopped dividing -- grouping sits after
        # a successful division and so can only ever widen a tree that is already moving.
        # If the level goes, there is nothing left here to divide.
        with log_context(stage="subdivision.split"):
            if await self._consider_split(folder, filename=filename, on_progress=on_progress):
                return []

        # Which folder is being judged is the first thing anyone reading these lines
        # needs, and it is not derivable from the document that triggered the call.
        with log_context(folder=str(folder) or "/"):
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

        # A folder born holding documents is asked about itself, once, before this
        # returns. "Every arrival asks" is not true for a folder that arrives full: the
        # documents that make it up were moved in, not filed in, so nothing asks it
        # again until something new happens to land there. Measured on 300 documents
        # every round: a shelf drawn holding 45 was still a leaf of 63 at the end,
        # having been asked exactly once in the whole run.
        #
        # The chain this used to cause -- a single ingest building 철학/현상학/체화된 인지,
        # one document per level -- is now refused before it reaches the filesystem: a
        # class that leaves fewer than two documents behind is NO_DIVISION, so a folder
        # cannot pass its contents down a level one at a time.
        # The list of signs here just changed, so the one question that can shorten it is
        # asked now and only now. Adding classes one at a time can widen a level and can
        # never narrow one, which left the width a folder reached early as the width it
        # kept for good (SPEC.md 3.3.1, and eight rounds of 300 documents: a root of 3,
        # then 4, then 22, decided by how broad the first two classes happened to be).
        if not divided.routed:
            with log_context(stage="subdivision.grouping"):
                await self._consider_grouping(folder, filename=filename, on_progress=on_progress)

        # A folder that arrived full is asked about itself, because its documents have
        # never been looked at together and nothing else will ask: they were moved in,
        # not filed in, so no arrival ever fires for them.
        #
        # Whether that continues downward is decided by where the documents went, not by
        # how deep we are. A shelf that emptied its parent has carried the whole problem
        # one level down and has to be asked again, or the archive keeps a 107-document
        # leaf. A shelf that took a few and left a pile behind has not: descending into
        # it lays down a corridor while the pile nobody divided sits at the top of it --
        # measured as four levels in a single ingest above 33 loose documents. The pile
        # is the more urgent question and it is already asked on every arrival.
        # Tightened once to "bigger than everything else here put together", to stop a
        # six-level corridor. It stopped the subdivision instead: 300 documents in five
        # folders, one leaf of 198, a width of 2. The corridor was never really about
        # this condition -- the same round asked that 198-document folder 233 times and
        # refused all 233 answers, which is what the refusal list above fixes.
        remaining = self._count_documents(folder, recursive=False)
        for child in divided.created:
            if self._count_documents(child, recursive=True) <= remaining:
                continue
            results = await self.consider(child, filename=filename, on_progress=on_progress)
            if results:
                return [divided, *results]
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
        # One child is a provisional shelf, not an established partition. Reviewing it
        # as a complete boundary made the inevitable "not useful navigation" verdict
        # trigger expensive redesigns before a second class had even emerged.
        established_boundary = len(contents.children) >= 2
        # There was a second door here: a folder whose loose pile outweighed its largest
        # child opened the review regardless of the schedule, because a one-child folder
        # is never "established" and so could never become due. The diagnosis was right
        # and the prescription was wrong. A pile that has not been divided is not a
        # boundary that was drawn wrongly -- redrawing it moves the documents that were
        # already filed and leaves the pile exactly where it was. Measured on 300
        # documents: 금융감독 및 시장질서 kept 30 loose behind a shelf of 6 through
        # every one of those reviews, while replacement took 48% of the run's model
        # calls, and in an earlier round the same path shattered a root into 23 thin
        # folders. Growing a new class out of the pile is the operation for that, and
        # it is asked on every arrival already. The corridor this door was opened for
        # is now reached by two operations that did not exist then: a folder born full
        # asks about itself, and a level that grew too wide can be narrowed again.
        if (
            charter is not None
            and charter.divided
            and established_boundary
            and charter.due_for_review(total)
            # Review safety problems postpone only the destructive review. They must
            # never prevent additive filing into the still-usable current structure.
            and len(self._read(folder, recursive=True).documents) == total
            and not self._has_protected_descendant(folder)
        ):
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
            with log_context(stage="subdivision.review"):
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
                with log_context(stage="subdivision.audit.current"):
                    current_audit = await self._audit_boundary(
                        folder=folder,
                        documents=review_contents.lines,
                        axis=charter.split_basis,
                        axis_question=charter.split_question,
                        groups=current_groups,
                        complete=True,
                    )
                    log_trace(
                        "subdivide.current_boundary_audit",
                        folder=str(folder),
                        accepted=current_audit.accepted,
                        failed_checks=_failed_boundary_checks(current_audit),
                    )
                current_boundary_holds = current_boundary_holds and current_audit.accepted
                observed_failures.extend(_failed_boundary_checks(current_audit))

            if not current_boundary_holds:
                with log_context(stage="subdivision.replacement"):
                    replacement_plan = await self._attempt_boundary_replacement(
                        folder=folder,
                        purpose=purpose,
                        charter=charter,
                        total=total,
                        documents=review_contents.lines,
                        current_groups=current_groups,
                        observed_failures=observed_failures,
                        children=direct_signs,
                        language=review_contents.language,
                    )
                if replacement_plan is not None:
                    return replacement_plan
                # A failed repair is state, not a reason to starve ordinary filing.
                # Persist the attempt so the same unchanged subtree is not redesigned
                # on every arrival, then continue into existing routing/emergence below.
                self._record_review_attempt(folder, charter, documents=total, repair_pending=True)
            # A holding review is still a judgement made at this size, and it has to be
            # recorded as one. Left unwritten, the folder stays past its doubling for
            # ever: it was asked on every ingest from then on -- fourteen times in a row
            # on one run, all of them holding -- and, worse, the answer returned here so
            # the folder was never asked what else had grown in it.
            else:
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

        # A charter that carries an axis has answered this already. Asking again while a
        # folder had fewer than two children put the question to the model 152 times in
        # one 300-document round and refused 145 of the answers for repeating an
        # ancestor's axis -- the documents had not changed, so neither had the answer.
        # Two children is what makes a boundary reviewable, not what makes an axis real.
        established = len(contents.children) >= 2
        axis = charter.split_basis if charter is not None and charter.divided else ""
        spent = self._axes_above(folder)

        with log_context(stage="subdivision.emerging"):
            emerging, gathered = await self._find_emerging(
                folder=folder,
                purpose=purpose,
                documents=contents.subject_lines,
                children=contents.children,
                axis=axis,
                # The same condition as the axis, deliberately. Split, the folder
                # inherited a property with no question attached, the chain skipped the
                # step that would have written one because the property was already
                # there, and the plan was refused for having no question -- 69 times.
                axis_question=(
                    charter.split_question if charter is not None and charter.divided else ""
                ),
                spent=spent,
                language=contents.language,
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

        # Only once nothing new has emerged. Routing used to run before that question and
        # drained the loose pile into the shelves that already existed, so a folder could
        # never grow a third class: measured at one root over 100 documents, 21 routings
        # against 17 chances to name something new, and a width frozen at two all run. The
        # pile is the evidence a new class is drawn from, so it is read for that first.
        if (
            not (emerging.emerged and emerging.name.strip())
            and charter is not None
            and charter.divided
            and charter.split_basis
            and charter.split_question
            and contents.children
        ):
            with log_context(stage="subdivision.routing"):
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
                    with log_context(stage="subdivision.routing.audit"):
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
        # ...but only once there is a boundary to be held to. ADR-0014 already calls a
        # single child provisional: it may receive documents and grow a sibling, and it is
        # not reviewed as a complete boundary. The axis was not provisional with it, and
        # that was the difference between a good tree and a bad one. Fixed from one class
        # drawn out of five documents, "what kind of document is this" is a locally correct
        # answer that then governs the whole archive for ever, because nothing short of a
        # destructive replacement can revise it. Both rounds that chose a format axis fixed
        # it at five documents; every subject axis was fixed at fifteen or more.
        #
        # So while the boundary is provisional the axis is asked again, with the existing
        # child shown. Nothing moves: the child keeps its name and its documents, and only
        # the recorded property changes. Two children make a boundary, and from then on the
        # axis is binding and only Review may redraw it.
        if not emerging.emerged or not emerging.name.strip():
            return None

        # Naming a shelf that already stands here is not a mistake, it is an answer to a
        # different question: these loose documents belong behind that sign. Refusing it
        # threw the answer away -- 119 times at one root in a 300-document round, which
        # is why 114 documents were still loose at the end of it. The sign it names is
        # already on the folder's axis, so nothing new is being decided; the documents
        # are asked one closed question each, exactly as routing does.
        existing = {normalise_label(name): (name, note) for name, note in contents.children}
        if (named := existing.get(normalise_label(emerging.name))) is not None:
            if charter is None or not charter.divided:
                return None
            with log_context(stage="subdivision.routing"):
                members = await self._find_members(
                    folder=folder,
                    purpose=purpose,
                    documents=contents.lines,
                    name=named[0],
                    # The sign already on that folder, which is what a reader would use.
                    sign=named[1],
                )
            log_trace(
                "subdivide.routed_to_named_sign",
                folder=str(folder),
                sign=named[0],
                documents=len(members.document_ids),
            )
            if not members.document_ids:
                return None
            return prompts.Division(
                basis=charter.split_basis,
                basis_question=charter.split_question,
                groups=[
                    prompts.Group(name=named[0], note=named[1], document_ids=members.document_ids)
                ],
                reuse_existing=True,
            )

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

        # A sign copied out of the pile it is labelling names those documents, not the
        # class, so it degrades to the derived form like any other unusable sign.
        offered_sign = "" if _quotes_evidence(emerging.sign, contents.lines) else emerging.sign
        note = _sign(
            offered_sign,
            axis=axis or emerging.axis.strip(),
            class_name=emerging.name,
            folder=folder,
        )
        # The handles the grouping step picked, standing in for a membership answer that
        # has not been asked yet. The audit is about the axis, the name and the sibling
        # names; it never needed to know which documents ended up behind the sign.
        audit_documents = contents.lines
        audit_groups = [prompts.Group(name=emerging.name, note=note, document_ids=list(gathered))]

        # The free checks before the paid one. Whether a name repeats its axis, carries an
        # ancestor's, spends an axis already used above, or is a path, is decided by
        # comparing strings -- and the audit is a model call with the folder's documents
        # in it. Run the other way round, 76 proposals in one round bought an audit before
        # code refused them for something it could see from the name alone.
        early = validate_names(
            axis=axis or emerging.axis.strip(),
            axis_question=(
                charter.split_question
                if charter is not None and charter.split_question and established
                else emerging.axis_question
            ),
            names=(emerging.name,),
            ancestor_names=folder.parts,
            spent_axes=tuple(spent),
        )
        if not early.accepted:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="; ".join(problem.value for problem in early.problems),
                proposed=[emerging.name],
            )
            return None
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
            for group in audit_groups:
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

        with log_context(stage="subdivision.audit"):
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

        # Only now, once the boundary has survived every check that does not depend on it,
        # is each document asked whether it belongs. This loop is one closed question per
        # document and it used to run first: 84% of the questions it asked in one round
        # were spent on proposals refused afterwards.
        with log_context(stage="subdivision.members"):
            members = await self._find_members(
                folder=folder,
                purpose=purpose,
                documents=contents.lines,
                name=emerging.name,
                sign=emerging.sign,
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
            prompts.Group(name=emerging.name, note=note, document_ids=members.document_ids)
        ]
        preview = validate_plan(
            axis=axis or emerging.axis.strip(),
            axis_question=(
                charter.split_question
                if charter is not None and charter.split_question and established
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

        return prompts.Division(
            # The axis, not a sentence about this one extraction. It is read back on the
            # next look and on review, and it is what holds the siblings to one question.
            basis=axis or emerging.axis.strip() or emerging.name,
            basis_question=(
                charter.split_question
                if charter is not None and charter.split_question and established
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
        merged: dict[str, list[str]] = {}
        handles = [f"F{index:03d}" for index in range(1, len(contents.children) + 1)]

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

        for document_id, choice in await _bounded_gather(contents.lines, decide):
            if choice in handles:
                merged.setdefault(choice, []).append(document_id)
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
        language: str = "",
    ) -> tuple[prompts.Emerging, tuple[str, ...]]:
        """Group, then name, then sign -- and the axis only the first time.

        One call used to decide all five. To name a class it had to be shown the
        enclosing folder's name, which is the one string it must not return, and the
        prompt spent three paragraphs and a capitalised line arguing against returning it
        anyway. gpt-5-nano returned it in 75 of 81 replies and the folder never divided;
        every refusal was correct and none of them built anything.

        So the steps are separated by what each may see. Grouping sees the documents
        without their doc_type, because that is the property it must not group on. Naming
        sees the chosen group and not the folder it sits in: a name that is never shown
        cannot be echoed, and narrowness comes from the input instead, the group being a
        strict subset. The axis is asked about the class rather than the folder, so the
        property already spent above is not the salient answer and no longer has to be
        listed in order to be forbidden.

        The guards behind all of this stay, and each says which guard it was when it
        fires. They are a backstop now rather than the mechanism.
        """

        def build(packet: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
            return prompts.build_group(
                documents=packet,
                children=children,
                axis=axis,
                axis_question=axis_question,
                language=language,
            )

        if _prompt_chars(build([])) > MAX_MAINTENANCE_PROMPT_CHARS:
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                reason="direct signs require boundary review before another class can emerge",
            )
            return prompts.Emerging(emerged=False), ()

        gathered: list[prompts.Gathered] = []
        for packet in _document_packets(documents, build):
            found = await self._llm.structured(build(packet), schema=prompts.Gathered)
            if kept := self._kept_members(found, packet, folder=folder):
                gathered.append(prompts.Gathered(members=kept, shared=found.shared.strip()))
        if not gathered:
            return prompts.Emerging(emerged=False), ()

        # The thickest, decided here rather than asked. The prompt already says to return
        # the thickest, and the reduce call that used to choose between candidates was one
        # more open question with one more way to answer it wrongly.
        chosen = max(gathered, key=lambda group: len(group.members))
        members = set(chosen.members)
        theirs = [line for line in documents if line[0] in members]

        # The question first, and asked from the group rather than from a name that does
        # not exist yet. A folder that already has one inherits it: either way the name is
        # then asked as an answer to a question, which is what a folder name is.
        settled_axis, question = axis, axis_question
        if not axis:
            asked = await self._llm.structured(
                prompts.build_axis(shared=chosen.shared, language=language),
                schema=prompts.Axis,
            )
            settled_axis, question = asked.axis.strip(), asked.axis_question.strip()
            if not question:
                _guard_refused("axis_without_a_question", folder=folder)
                return prompts.Emerging(emerged=False), ()

        named = await self._llm.structured(
            prompts.build_class_name(
                shared=chosen.shared,
                question=question,
                documents=theirs,
                taken=[name for name, _ in children],
                language=language,
            ),
            schema=prompts.ClassName,
        )
        name = " ".join(named.name.split()).strip()
        if not name:
            _guard_refused("class_name_empty", folder=folder)
            return prompts.Emerging(emerged=False), ()

        signed = await self._llm.structured(
            prompts.build_class_sign(shared=chosen.shared, language=language),
            schema=prompts.ClassSign,
        )
        if not signed.sign.strip():
            _guard_refused("sign_empty", folder=folder, name=name)

        log_trace(
            "subdivide.gathered",
            folder=str(folder),
            of=len(documents),
            took=len(members),
            name=name,
            axis_is_new=not axis,
        )
        return (
            prompts.Emerging(
                emerged=True,
                name=name,
                sign=signed.sign.strip(),
                axis=settled_axis,
                axis_question=question,
            ),
            tuple(chosen.members),
        )

    def _kept_members(
        self, group: prompts.Gathered, packet: list[tuple[str, str]], *, folder: PurePosixPath
    ) -> list[str]:
        """The handles a group may keep: real, distinct, at least two, not all of them.

        Structural, not semantic. The model is not second-guessed about which documents
        belong together, only about whether it answered with documents that were shown
        and left something behind.
        """
        shown = [handle for handle, _ in packet]
        kept = [handle for handle in dict.fromkeys(group.members) if handle in shown]
        if invented := len(group.members) - len(kept):
            _guard_refused("group_handle_unknown", folder=folder, count=invented)
        if not kept:
            return []
        if len(kept) < 2:
            _guard_refused("group_too_small", folder=folder, count=len(kept))
            return []
        if len(kept) == len(shown):
            _guard_refused("group_took_everything", folder=folder, count=len(kept))
            return []
        if not group.shared.strip():
            _guard_refused("group_without_a_sentence", folder=folder)
            return []
        return kept

    async def _find_members(
        self,
        *,
        folder: PurePosixPath,
        purpose: str,
        documents: list[tuple[str, str]],
        name: str,
        sign: str = "",
    ) -> prompts.Members:
        async def decide(document: tuple[str, str]) -> tuple[str, str]:
            choice = await self._llm.choose(
                prompts.build_member_choice(
                    path=str(folder),
                    purpose=purpose,
                    document=document,
                    name=name,
                    sign=sign,
                ),
                choices=("SHELF", "STAY"),
            )
            return document[0], choice

        decisions = await _bounded_gather(documents, decide)
        return prompts.Members(
            document_ids=[document_id for document_id, choice in decisions if choice == "SHELF"]
        )

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

        async def ask(
            packet: list[tuple[str, str]], signs: list[tuple[str, str]]
        ) -> prompts.Review:
            """One packet, one closed question per check."""
            verdicts: dict[str, bool] = {}
            for name, question in prompts.REVIEW_CHECKS:
                answer = await self._llm.choose(
                    prompts.build_review_check(
                        check=question,
                        path=str(folder),
                        purpose=purpose,
                        basis=charter.split_basis,
                        basis_question=charter.split_question,
                        before=charter.split_at_documents,
                        count=total,
                        documents=packet,
                        children=signs,
                    ),
                    choices=("FAILS", "HOLDS"),
                )
                # An unusable reply is not evidence that the boundary failed.
                verdicts[name] = answer.strip().upper() != "FAILS"
            return prompts.Review(**verdicts)

        for position, signs in enumerate(sign_packets, start=1):
            with log_context(window_id=f"review:signs-{position:03d}"):
                checks.append(await ask([], signs))
        for index, packet in enumerate(document_packets, start=1):
            signs = _relevant_children(packet, children) if sign_packets else children
            # A boolean merged fail-closed from many packets is unreadable unless each
            # packet's own answer can be found.
            with log_context(window_id=f"review:docs-{index:03d}"):
                checks.append(await ask(packet, signs))
            log_trace(
                "subdivide.review_packet",
                folder=str(folder),
                packet=index,
                packets=len(document_packets),
                documents=len(packet),
            )
        merged = prompts.Review(
            one_axis=_carried(check.one_axis for check in checks),
            coherent_membership=_carried(check.coherent_membership for check in checks),
            useful_navigation=_carried(check.useful_navigation for check in checks),
        )
        log_trace(
            "subdivide.review_merge",
            folder=str(folder),
            packets=len(checks),
            one_axis_failed=sum(1 for check in checks if not check.one_axis),
            membership_failed=sum(1 for check in checks if not check.coherent_membership),
            navigation_failed=sum(1 for check in checks if not check.useful_navigation),
            holds=merged.holds,
        )
        return merged

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
        language: str = "",
    ) -> prompts.Division | None:
        """Return a fully validated replacement, without controlling later filing."""
        replacement = await self._propose_replacement(
            folder=folder,
            purpose=purpose,
            charter=charter,
            total=total,
            documents=documents,
            children=children,
            language=language,
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
            # Not require_complete. A replacement redraws the boundary; it does not
            # have to account for every document, and demanding that is what forced
            # unrelated documents behind whichever new name sounded broadest.
            require_complete=False,
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
        language: str = "",
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
                language=language,
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
        for position, signs in enumerate(sign_packets, start=1):
            with log_context(window_id=f"sketch:signs-{position:03d}"):
                sketch = await self._llm.structured(
                    build_sketch([], signs),
                    schema=prompts.ReplacementSketch,
                )
            sketches.append(_normalise_sketch(sketch))
        for index, packet in enumerate(document_packets, start=1):
            signs = _relevant_children(packet, children) if sign_packets else children
            # Reduction is lossy, so the pre-reduce sketch of each packet has to survive
            # somewhere readable.
            with log_context(window_id=f"sketch:docs-{index:03d}"):
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
            for position, batch in enumerate(batches, start=1):
                if len(batch) == 1:
                    reduced.extend(batch)
                    continue
                with log_context(window_id=f"sketch:reduce-{len(sketches):03d}-{position:03d}"):
                    result = await self._llm.structured(
                        prompts.build_replacement_reduce(path=str(folder), sketches=batch),
                        schema=prompts.ReplacementSketch,
                    )
                reduced.append(_normalise_sketch(result))
            sketches = reduced

        sketch = sketches[0]
        # The sign too, not only the axis and the name: a round that passed this check on
        # both still wrote four law titles into a sign, because that was the field nobody
        # was looking at.
        quoted = [
            wording
            for wording in [
                sketch.basis,
                *(sign.name for sign in sketch.signs),
                *(sign.sign for sign in sketch.signs),
            ]
            if _quotes_evidence(wording, documents)
        ]
        if quoted:
            # Refused before the per-document assignment calls, which are the expensive
            # part and would only distribute documents under a copied label.
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="replacement wording is copied from the documents it is sorting",
                proposed=quoted,
                replacement=True,
            )
            return None

        assignments_by_sign: list[list[str]] = [[] for _ in sketch.signs]
        handles = [f"G{index:03d}" for index in range(1, len(sketch.signs) + 1)]

        async def decide(document: tuple[str, str]) -> tuple[str, str]:
            choice = await self._llm.choose(
                prompts.build_replacement_choice(
                    path=str(folder), document=document, sketch=sketch
                ),
                choices=[*handles, "STAY"],
            )
            return document[0], choice

        decisions = await _bounded_gather(documents, decide)
        stayed = 0
        for document_id, handle in decisions:
            # A document that fits none of the new signs keeps the folder it is in. The
            # replacement is still a redrawing of the boundary; it is just no longer a
            # partition, and a partition of a heterogeneous pile can only be completed by
            # inventing a residue class (see this module's prompts docstring).
            if handle.strip().upper() == "STAY":
                stayed += 1
                continue
            if handle not in handles:
                log_trace(
                    "subdivide.rejected",
                    folder=str(folder),
                    reason="replacement assignment returned an unknown sign",
                    document=document_id,
                    replacement=True,
                )
                return None
            assignments_by_sign[int(handle[1:]) - 1].append(document_id)
        if stayed:
            log_trace(
                "subdivide.replacement_left_behind",
                folder=str(folder),
                stayed=stayed,
                assigned=len(decisions) - stayed,
                basis=sketch.basis,
            )

        return prompts.Replacement(
            basis=sketch.basis,
            basis_question=sketch.basis_question,
            groups=[
                prompts.Group(
                    name=sign.name,
                    note=_sign(
                        sign.sign,
                        axis=sketch.basis,
                        class_name=sign.name,
                        folder=folder,
                    ),
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

        # The property is checked on its own, because it is the one judgement that
        # survives every later question about this folder and the only one that was
        # answered wrongly every single time inside the combined reply.
        axis_holds = "HOLDS"
        if groups:
            axis_holds = await self._llm.choose(
                prompts.build_axis_check(
                    path=str(folder),
                    axis=axis,
                    axis_question=axis_question,
                    names=[group.name for group in groups],
                    spent=list(self._axes_above(folder)),
                ),
                choices=("FAILS", "HOLDS"),
            )
        if axis_holds.strip().upper() == "FAILS":
            log_trace(
                "subdivide.axis_refused",
                folder=str(folder),
                axis=axis,
                proposed=[group.name for group in groups],
            )
            return prompts.BoundaryAudit(
                one_property=True,
                names_answer_question=True,
                mutually_exclusive=True,
                useful_for_navigation=False,
            )

        checks: list[prompts.BoundaryAudit] = []
        if _prompt_chars(build([], groups)) > MAX_MAINTENANCE_PROMPT_CHARS:
            group_packets = _value_packets(groups, lambda shown: build([], shown))
            for shown in group_packets:
                checks.append(
                    await self._llm.structured(build([], shown), schema=prompts.BoundaryAudit)
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
                    await self._llm.structured(build(packet, shown), schema=prompts.BoundaryAudit)
                )
        else:
            checks = [
                await self._llm.structured(build(packet), schema=prompts.BoundaryAudit)
                for packet in _document_packets(documents, build)
            ]
        return prompts.BoundaryAudit(
            **{
                name: all(getattr(check, name) for check in checks)
                for name, _ in prompts.BOUNDARY_CHECKS
            }
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
                # Show the sign that is actually on disk. Substituting the derived form
                # here meant review judged a boundary by signs no reader would ever see.
                routing_sign(charter.purpose, axis=parent.split_basis, class_name=child.name)
                if (parent := self._charter(folder)) is not None
                and parent.divided
                and charter is not None
                and charter.managed
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
        placed: list[tuple[str, PurePosixPath]] = []
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
            for document_id, path in members:
                operations.extend(self._move_document(path, target))
                placed.append((document_id, target))
                moved += 1

            child_charter = Charter(
                path=target,
                title=name,
                # The sign the plan was audited with, not a fresh derivation of it. A
                # boundary that passed on one wording must go to disk with that wording.
                purpose=_sign(group.note, axis=plan.basis, class_name=name, folder=folder),
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
        self._log_moves(folder, placed)
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
            # Not require_complete. A replacement redraws the boundary; it does not
            # have to account for every document, and demanding that is what forced
            # unrelated documents behind whichever new name sounded broadest.
            require_complete=False,
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
                purpose=_sign(group.note, axis=plan.basis, class_name=target.name, folder=folder),
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

        # Everything was staged; only what a group claimed has come back out. A document
        # that answered STAY has to be put down again or it is lost -- staged under
        # .bismuth, invisible to the vault and to its owner. Measured: eight of a hundred
        # documents stranded the first time a replacement was allowed to leave any behind.
        #
        # It lands in the folder itself, loose. Its old sub-folder was retired above with
        # the rest of the boundary, and "stays where it is" means the folder being
        # redrawn -- which is exactly what the loose pile is.
        claimed = {document_id for group in plan.groups for document_id in group.document_ids}
        left = [(key, paths) for key, paths in staged.items() if key not in claimed]
        loose: set[str] = set()
        for _, (staged_document, staged_sidecar) in left:
            filename = _free_filename(staged_document.name.split("-", 1)[1], loose)
            loose.add(filename.casefold())
            operations.append(
                Operation(
                    kind=OperationKind.MOVE,
                    source=staged_document,
                    target=folder / filename,
                    note="return document that fits no replacement class",
                )
            )
            if staged_sidecar is not None:
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=staged_sidecar,
                        target=folder / sidecar_name(filename),
                        note="return sidecar with its document",
                    )
                )
        if left:
            log_trace(
                "subdivide.replacement_left_loose",
                folder=str(folder),
                returned=len(left),
                claimed=len(claimed),
            )

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

    async def _consider_split(
        self,
        folder: PurePosixPath,
        *,
        filename: str,
        on_progress: ProgressSink | None,
    ) -> bool:
        """Ask whether this level earns the guess it costs, and dissolve it if not.

        The reverse of :meth:`_consider_grouping`, and the operator this library did not
        have. Without it a level, once drawn, is permanent: one branch reached seven
        levels, six of whose seven segments contained 금융, and every one of them had been
        locally justified when it was drawn (ADR-0018).

        Like grouping it moves folders, never documents. Every document keeps the folder
        it is in and the path above it gets shorter by one, so a wrong answer here costs a
        level rather than a scrambled collection -- which is why it can be asked at all.
        """
        if not folder.parts:
            return False
        parent = folder.parent
        charter = self._charters.load(folder)
        if charter is None or not charter.managed or self._has_protected_descendant(folder):
            return False

        contents = self._read(folder)
        children = [
            (name, note, self._count_documents(folder / name, recursive=True))
            for name, note in contents.children
            if name != INBOX.parts[0]
        ]
        promoted = tuple(name for name, _, _ in children)
        here = len(contents.documents)
        if not promoted and not here:
            return False

        parent_contents = self._read(parent)
        siblings = [(name, note) for name, note in parent_contents.children if name != folder.name]
        parent_charter = self._charters.load(parent)

        validation = validate_split(
            promoted=promoted,
            ancestor_names=parent.parts,
            taken=tuple(name for name, _ in siblings),
            documents=here,
        )
        if not validation.accepted:
            _guard_refused(
                "split_unsafe",
                folder=folder,
                reason="; ".join(problem.value for problem in validation.problems),
            )
            return False

        answer = await self._llm.choose(
            prompts.build_split_check(
                path=str(folder),
                note=charter.purpose,
                children=children,
                documents=here,
                parent=str(parent),
                parent_note=parent_charter.purpose if parent_charter else "",
                siblings=siblings,
                language=contents.language,
            ),
            choices=("DISSOLVE", "KEEP"),
        )
        log_trace(
            "subdivide.split_asked",
            folder=str(folder),
            children=len(children),
            documents=here,
            answer=answer,
        )
        if answer.strip().upper() != "DISSOLVE":
            return False

        report(on_progress, Progress(stage=Stage.DIVIDING, filename=filename, note=str(parent)))
        return self._apply_split(folder, children)

    def _apply_split(self, folder: PurePosixPath, children: list[tuple[str, str, int]]) -> bool:
        """Move everything one step up and remove the level, in one undoable batch."""
        parent = folder.parent
        operations: list[Operation] = []
        moved = 0

        for child_name, _, _ in children:
            source = folder / child_name
            subtree = sorted(
                (f for f in self._vault.iter_folders() if _within(f, source)),
                key=lambda f: len(f.parts),
            )
            for sub in subtree:
                destination = (
                    parent / child_name
                    if sub == source
                    else parent / child_name / sub.relative_to(source)
                )
                operations.append(Operation(kind=OperationKind.MKDIR, target=destination))
                for path in sorted(self._vault.iter_files(sub, recursive=False)):
                    operations.extend(self._move_document(path, destination))
                    moved += 1
                note = sub / CHARTER_FILENAME
                if self._vault.exists(note):
                    operations.append(
                        Operation(
                            kind=OperationKind.MOVE,
                            source=note,
                            target=destination / CHARTER_FILENAME,
                            note="folder note",
                        )
                    )
            operations.extend(
                Operation(kind=OperationKind.RMDIR, target=path) for path in reversed(subtree)
            )

        # This level's own documents come up too; nothing is left behind to strand the
        # rmdir, and no document is ever staged anywhere.
        for path in sorted(self._vault.iter_files(folder, recursive=False)):
            operations.extend(self._move_document(path, parent))
            moved += 1
        note = folder / CHARTER_FILENAME
        if self._vault.exists(note):
            operations.append(
                Operation(kind=OperationKind.REMOVE, target=note, note="retired folder note")
            )
        operations.append(Operation(kind=OperationKind.RMDIR, target=folder))

        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=(
                    f"dissolve {folder} into {parent or '/'}: "
                    f"{len(children)} folder(s), {moved} file(s) move up"
                ),
                operations=tuple(operations),
            )
        )
        log_trace(
            "subdivide.split",
            folder=str(folder),
            into=str(parent),
            promoted=[name for name, _, _ in children],
            files=moved,
        )
        return True

    async def _consider_grouping(
        self,
        folder: PurePosixPath,
        *,
        filename: str,
        on_progress: ProgressSink | None,
    ) -> bool:
        """Ask whether several sub-folders that already exist belong on one shelf.

        The fourth operation, and the only one that moves a folder instead of a document
        (docs/spec/subdivision.md 2). No document changes the folder it is in; the path
        above it changes. That is why this can be asked freely where redrawing a boundary
        cannot: there is nothing here for a wrong answer to scramble, only a level to add
        or not add.
        """
        charter = self._charters.load(folder)
        if charter is None or not charter.managed or self._has_protected_descendant(folder):
            return False
        children = [
            (name, note, self._count_documents(folder / name, recursive=True))
            for name, note in self._read(folder).children
            if name != INBOX.parts[0]
        ]
        if len(children) < 3:
            # Two folders cannot be tidied into one shelf and still leave one standing
            # here, so there is no answer this question could have.
            return False

        proposal = await self._llm.structured(
            prompts.build_grouping(
                path=str(folder),
                children=children,
                axis=charter.split_basis,
                language=self._read(folder).language,
            ),
            schema=prompts.Grouping,
        )
        log_trace(
            "subdivide.grouping",
            folder=str(folder),
            emerged=proposal.emerged,
            name=proposal.name,
            children=len(children),
        )
        if not proposal.emerged or not proposal.name.strip():
            return False

        members: list[tuple[str, str, int]] = []
        for child in children:
            answer = await self._llm.choose(
                prompts.build_grouping_member(
                    path=str(folder), name=proposal.name, sign=proposal.sign, child=child
                ),
                choices=("SHELF", "STAY"),
            )
            if answer.strip().upper() == "SHELF":
                members.append(child)

        validation = validate_grouping(
            name=proposal.name,
            axis=charter.split_basis,
            members=tuple(name for name, _, _ in members),
            siblings=tuple(name for name, _, _ in children),
            ancestor_names=folder.parts,
        )
        if not validation.accepted:
            log_trace(
                "subdivide.grouping_rejected",
                folder=str(folder),
                reason="; ".join(problem.value for problem in validation.problems),
                proposed=proposal.name,
                members=[name for name, _, _ in members],
            )
            return False
        report(
            on_progress,
            Progress(stage=Stage.DIVIDING, filename=filename, note=str(folder / proposal.name)),
        )
        return self._apply_grouping(folder, charter, proposal, members)

    def _apply_grouping(
        self,
        folder: PurePosixPath,
        charter: Charter,
        proposal: prompts.Grouping,
        members: list[tuple[str, str, int]],
    ) -> bool:
        """Move whole sub-folders under one new shelf, in a single undoable batch."""
        try:
            name = sanitize_segment(proposal.name)
        except ValueError:
            log_trace(
                "subdivide.grouping_rejected",
                folder=str(folder),
                reason="unusable path segment",
                proposed=proposal.name,
            )
            return False
        target = folder / name
        if self._vault.exists(target):
            return False

        operations: list[Operation] = [Operation(kind=OperationKind.MKDIR, target=target)]
        payloads: dict[PurePosixPath, bytes] = {}
        emptied: list[PurePosixPath] = []
        moved = 0
        for child_name, _, _ in members:
            source = folder / child_name
            # Shallowest first, so a folder is created before anything lands in it.
            subtree = sorted(
                (f for f in self._vault.iter_folders() if _within(f, source)),
                key=lambda f: len(f.parts),
            )
            for sub in subtree:
                destination = (
                    target / child_name
                    if sub == source
                    else target / child_name / sub.relative_to(source)
                )
                operations.append(Operation(kind=OperationKind.MKDIR, target=destination))
                for path in sorted(self._vault.iter_files(sub, recursive=False)):
                    # Sidecars travel with their document, as everywhere else.
                    operations.extend(self._move_document(path, destination))
                    moved += 1
                # The folder note is not a document, so it is not in iter_files -- and a
                # folder that still holds its own note is not empty, so leaving it behind
                # would strand the note and block the rmdir.
                note = sub / CHARTER_FILENAME
                if self._vault.exists(note):
                    operations.append(
                        Operation(
                            kind=OperationKind.MOVE,
                            source=note,
                            target=destination / CHARTER_FILENAME,
                            note="folder note",
                        )
                    )
            # Deepest first: a folder is only removable once everything under it has gone.
            emptied.extend(reversed(subtree))
        operations.extend(Operation(kind=OperationKind.RMDIR, target=path) for path in emptied)

        # The shelf answers the same question its contents answer, one step up, so it
        # carries the parent's axis rather than inventing one. It is divided from birth:
        # the folders standing in it are its boundary.
        shelf = Charter(
            path=target,
            title=name,
            purpose=_sign(proposal.sign, axis=charter.split_basis, class_name=name, folder=folder),
            split_basis=charter.split_basis,
            split_question=charter.split_question,
            split_at_documents=moved,
            holds=(),
            answers=(),
        )
        operations.append(
            Operation(
                kind=OperationKind.WRITE, target=target / CHARTER_FILENAME, note="folder note"
            )
        )
        payloads[target / CHARTER_FILENAME] = shelf.to_markdown().encode("utf-8")

        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=(
                    f"group {len(members)} folder(s) of {folder or '/'} under {name} "
                    f"({moved} file(s))"
                ),
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        log_trace(
            "subdivide.grouped",
            folder=str(folder),
            shelf=str(target),
            members=[child_name for child_name, _, _ in members],
            files=moved,
        )
        return True

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
            # Fill in a missing or unusable sign; never overwrite a usable one. This
            # migrated every managed child to the derived form, which is how a whole
            # archive ended up with signs that repeat their own folder name.
            purpose = routing_sign(charter.purpose, axis=axis, class_name=child.name)
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
            subject = _describe(card, with_type=False)
            if recursive:
                relative = path.relative_to(folder) if folder.parts else path
                description = f"current={relative} | {description}"
                subject = f"current={relative} | {subject}"
            contents.documents.append((document_id, description, path))
            contents.subjects.append((document_id, subject))
            if script := _writing_system(card.title):
                contents.scripts.append(script)
            if code := card.language.strip():
                contents.languages.append(code.casefold())

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
                        note = routing_sign(
                            loaded.purpose, axis=parent.split_basis, class_name=child.name
                        )
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

    def _log_moves(self, folder: PurePosixPath, moves: list[tuple[str, PurePosixPath]]) -> None:
        """Record which documents a subdivision moved, and where each one went.

        The applied/routed events carry a count, and the document_id on them is the
        arrival that triggered the pass -- not the documents that were swept. Measured on
        a 165-document vault: 19 moves were attributable and 186 were not, so "why is this
        document here" had no answer for nine of every ten documents, which is the chain
        SPEC.md 6.3 requires to stay joinable.
        """
        for document_id, target in moves:
            log_trace(
                "document.moved",
                document_id=document_id,
                from_folder=str(folder),
                to_folder=str(target),
            )

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


def _normalise(text: str) -> str:
    return "".join(text.split()).casefold()


def _sign(proposed: str, *, axis: str, class_name: str, folder: PurePosixPath) -> str:
    """The note that goes on disk, and a line in the log when it is not the model's.

    The fallback repeats the folder name in other words and rules nothing out, so a run
    that writes it often has a defect worth finding. Without this line the only evidence
    was the shape of the note itself, read off the finished vault by hand.
    """
    if (refusal := sign_refusal(proposed, class_name=class_name)) is not None:
        log_trace(
            "subdivide.sign_refused",
            folder=str(folder),
            name=class_name,
            reason=refusal,
            proposed=proposed[:160],
        )
    return routing_sign(proposed, axis=axis, class_name=class_name)


def _within(candidate: PurePosixPath, root: PurePosixPath) -> bool:
    """Whether ``candidate`` is ``root`` or sits under it."""
    return candidate == root or candidate.parts[: len(root.parts)] == root.parts


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
    if len(contents.scripts) < 2:
        return None
    source_counts = Counter(contents.scripts)
    source_script, source_count = source_counts.most_common(1)[0]
    if source_count / len(contents.scripts) < 0.75:
        return None

    wording = " ".join([plan.basis, plan.basis_question] + [group.name for group in plan.groups])
    proposed_script = _writing_system(wording)
    if proposed_script is not None and proposed_script != source_script:
        return "boundary wording uses a different writing system from its documents"
    return None


# Compatibility for embedders that used the alpha API. New code should name the role,
# not the one operation the first implementation happened to support.
SubdivisionService = LibraryMaintenanceService


def _guard_refused(guard: str, *, folder: PurePosixPath, **fields: object) -> None:
    """One line per safety net that fired, under one event name.

    A guard that only refuses is invisible: a run where nothing was built and a run where
    everything was built look the same in a folder count. Named here so a finished run can
    be asked which nets caught what, and how often -- a net that catches on most calls is
    not protecting the design, it is the design.
    """
    log_trace("guard.refused", guard=guard, folder=str(folder), **fields)


def _describe(card: DocumentCard, *, with_type: bool = True) -> str:
    """The card evidence used for grouping; never the original document bytes."""
    topics = ", ".join(card.topics)
    parts = [card.title, card.doc_type] if with_type else [card.title]
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
    *,
    max_documents: int | None = None,
) -> list[list[tuple[str, str]]]:
    """Partition evidence by input context and, when needed, output cardinality."""
    if max_documents is not None and max_documents < 1:
        raise ValueError("max_documents must be positive")
    if not documents:
        if _prompt_chars(build([])) > MAX_MAINTENANCE_PROMPT_CHARS:
            raise BismuthError("maintenance metadata exceeds context budget")
        return [[]]
    packets: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for document in documents:
        candidate = [*current, document]
        output_would_overflow = bool(
            current and max_documents is not None and len(candidate) > max_documents
        )
        if current and (
            output_would_overflow or _prompt_chars(build(candidate)) > MAX_MAINTENANCE_PROMPT_CHARS
        ):
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


def _quotes_evidence(wording: str, documents: list[tuple[str, str]]) -> bool:
    """Whether a proposed axis or name is copied out of the documents in front of it.

    An axis is the name of a property; a document title is a value of nothing. Observed
    live: a replacement returned the axis "소상공인 보호 및 지원에 관한 법률, 공인회계사법,
    보험업법 시행령, 서민의 금융생활 지원에 관한 법률" -- four titles from the packet,
    joined by commas, recorded on the folder and shown to every later question about it.

    No vocabulary: the comparison is against the evidence in the same request, so it
    means the same thing in any language and for any collection. Short titles are
    skipped because a two-character title carries no evidence of copying.
    """
    text = normalise_label(wording)
    if not text:
        return False
    for _, description in documents:
        title = description.split(" | ")[0].removeprefix("current=").strip()
        key = normalise_label(title)
        if len(key) >= 8 and key in text:
            return True
    return False


def _carried(votes: Iterable[bool]) -> bool:
    """Whether a check survives across the packets a review was split into.

    Not ``all``. Each packet judges the whole boundary from its own slice of the
    documents, so one packet's doubt is one slice's doubt -- and a failed review is not
    a no-op, it triggers a complete replacement of the boundary. Requiring unanimity to
    hold therefore made the most destructive operation in the system the easiest one to
    reach: in a 300-document run every one of 13 reviews failed, and the replacements
    turned working sign names into truncated sentences.

    Fail-closed belongs on mutation, not here. The conservative answer to "should this
    boundary be torn down" is no, so a majority of the evidence has to say yes.
    """
    counted = list(votes)
    if not counted:
        return True
    return sum(counted) * 2 > len(counted)


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
    return sketch


async def _bounded_gather(
    documents: list[tuple[str, str]],
    worker: Callable[[tuple[str, str]], Awaitable[tuple[str, str]]],
) -> list[tuple[str, str]]:
    """Run small independent choices with bounded pressure on a local model server."""
    semaphore = asyncio.Semaphore(4)

    async def run(document: tuple[str, str]) -> tuple[str, str]:
        async with semaphore:
            return await worker(document)

    return list(await asyncio.gather(*(run(document) for document in documents)))


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
            "each_name_is_one_answer",
            "subject_before_attribute",
        )
        if not getattr(audit, name)
    ]


def _failed_routing_checks(audit: prompts.RoutingAudit) -> list[str]:
    return [
        name for name in ("assignments_match_signs", "no_forced_fit") if not getattr(audit, name)
    ]
