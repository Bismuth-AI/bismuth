"""CREATE: drawing one class out of a pile, and routing a loose document behind a sign that stands."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from bismuth.domain.charter import (
    CHARTER_FILENAME,
    Charter,
)
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.maintenance import (
    ProposedClass,
    normalise_label,
    validate_names,
    validate_plan,
)
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_context, log_trace
from bismuth.prompts import subdivision as prompts
from bismuth.services.subdivision.naming import (
    _boundary_wording_problem,
    _guard_refused,
    _same_axis,
    _same_name,
    _sign,
)
from bismuth.services.subdivision.reading import (
    MAX_MAINTENANCE_PROMPT_CHARS,
    Divided,
    _bounded_gather,
    _Contents,
    _document_packets,
    _prompt_chars,
    _quotes_evidence,
    _vocabulary,
)
from bismuth.services.subdivision.shared import NeedsAFolder

logger = logging.getLogger(__name__)


class DrawsAClass(NeedsAFolder):
    """CREATE: drawing one class out of a pile, and routing a loose document behind a sign that stands.

    A mixin: it reads the collaborators and the memories that
    :class:`~bismuth.services.subdivision.service.LibraryMaintenanceService` sets up,
    and is only ever used through it.
    """

    async def _judge(
        self,
        folder: PurePosixPath,
        contents: _Contents,
        charter: Charter | None,
        *,
        filename: str,
        on_progress: ProgressSink | None,
        allow_emerging: bool,
        may_create: bool = True,
    ) -> prompts.Division | None:
        """Ask the model. Returns None when there is nothing to ask about."""
        purpose = charter.purpose if charter else ""
        # Through the subtree: dividing moves this folder's documents into its children,
        # so a direct count collapses to nothing and the division is never looked at again.
        total = self._count_documents(folder, recursive=True)

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
        # Only when a property is being chosen. Once it is recorded it is a fact about
        # this folder, and holding it against the ancestors again refuses the folder's
        # own settled boundary: a shelf built by grouping carries its parent's property
        # deliberately -- the folders standing in it are answers to it -- so every class
        # it went on to propose was refused for reusing it. Measured at 76 refusals in
        # one run, with 77 documents left loose in the shelf that could not divide.
        spent = self._axes_above(folder) if not axis else []

        if not may_create:
            # The pile cannot give up a class and still leave one, or the level below this
            # one would be past the depth a reader can follow. Routing still runs: putting
            # a loose document behind a sign that already stands here creates nothing.
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                reason="no class could legally come out of this pile",
                documents=len(contents.documents),
            )
            emerging = prompts.Emerging(emerged=False)
        else:
            emerging = await self._emerging(
                folder=folder,
                purpose=purpose,
                contents=contents,
                charter=charter,
                axis=axis,
                spent=spent,
                total=total,
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
                    return prompts.Division(
                        basis=charter.split_basis,
                        basis_question=charter.split_question,
                        groups=resolved_groups,
                        reuse_existing=True,
                    )
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
        # The free checks before the paid one. Whether a name repeats its axis, carries an
        # ancestor's, spends an axis already used above, or is a path, is decided by
        # comparing strings -- and the audit is a model call with the folder's documents
        # in it. Run the other way round, 76 proposals in one round bought an audit before
        # code refused them for something it could see from the name alone.
        early = validate_names(
            taken_anywhere=self._names_in_use(),
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

        # Only now, once the boundary has survived every check that does not depend on it,
        # is each document asked whether it belongs. This loop is one closed question per
        # document and it used to run first: 84% of the questions it asked in one round
        # were spent on proposals refused afterwards.
        loose = len(contents.documents)
        if self._asked_before(folder, emerging.name, documents=loose):
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                reason="this name already shelved nothing here",
                proposed=[emerging.name],
                documents=loose,
            )
            return None
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
            self._bought_nothing(folder, emerging.name, documents=loose)
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
            depth=len(folder.parts),
        )
        if not preview.accepted:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="; ".join(problem.value for problem in preview.problems),
                proposed=[group.name for group in proposed_groups],
            )
            # The loop has already been paid for, and the same name on the same pile will
            # buy the same refusal: 하도급거래 공정화에 관한 법률 claimed exactly one
            # document 21 times over, each time refused as a class of one.
            self._bought_nothing(folder, emerging.name, documents=loose)
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

    async def _emerging(
        self,
        *,
        folder: PurePosixPath,
        purpose: str,
        contents: _Contents,
        charter: Charter | None,
        axis: str,
        spent: list[str],
        total: int,
    ) -> prompts.Emerging:
        """The chain that names one class, lifted out so the gate above has a step to skip."""
        with log_context(stage="subdivision.emerging"):
            emerging, _ = await self._find_emerging(
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
        return emerging

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

        # A folder that answers "all of these belong together" is not remembered as
        # settled. That answer breaks the one contract this step has -- a group must leave
        # a remainder -- so the guard refuses it, and a refused answer is not a finding
        # about the folder. Remembered as one, it locked the biggest piles out of being
        # asked at all: 67 divisions blocked to save 8 calls, and every folder the spec
        # counted as an undivided pile was one this memory had shut. The chain stops at
        # the grouping call, which is the cheapest in it, so being wrong here is cheap and
        # not asking is not.
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
        rest = _vocabulary(self._read(folder), taken=members) if not axis else []
        if not axis:
            asked = await self._llm.structured(
                prompts.build_axis(
                    shared=chosen.shared,
                    rest=rest,
                    language=language,
                    is_root=not folder.parts,
                ),
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

        # Only when the property is being fixed. Once a folder is divided every later
        # class answers a question that has already been checked, and re-checking it on
        # every proposal is what made this expensive when it lived inside the audit.
        if not axis:
            verdict = await self._llm.choose(
                prompts.build_axis_check(
                    path=str(folder),
                    axis=settled_axis,
                    axis_question=question,
                    name=name,
                    # The same evidence the property was chosen from, so the two rules
                    # about what the documents here would answer have something to read.
                    rest=rest,
                    spent=spent,
                ),
                choices=("FAILS", "HOLDS"),
            )
            if verdict.strip().upper() == "FAILS":
                log_trace(
                    "subdivide.axis_refused",
                    folder=str(folder),
                    axis=settled_axis,
                    proposed=[name],
                )
                return prompts.Emerging(emerged=False), ()

        # The property was checked when it was chosen, and never again. Nothing then read
        # the names it was supposed to be producing, so a folder divided on 적용 대상 --
        # who the law applies to -- grew 중대재해처벌법 and 테러자금금지법 as answers.
        # Asked here, before the sign is written and long before the membership loop.
        #
        # Once per name and question, though. The same name was proposed and turned down
        # nine times under one question in a single run, and the check has no way to
        # answer differently the ninth time: it reads the name and the question, and
        # neither has changed.
        refused_here = self._not_an_answer.setdefault(
            (str(folder), normalise_label(question)), set()
        )
        if normalise_label(name) in refused_here:
            log_trace(
                "subdivide.name_refused",
                folder=str(folder),
                axis=settled_axis,
                question=question,
                proposed=[name],
                remembered=True,
            )
            return prompts.Emerging(emerged=False), ()

        answers = await self._llm.choose(
            prompts.build_name_check(
                path=str(folder),
                question=question,
                name=name,
                taken=[child for child, _ in children],
            ),
            choices=("ANSWERS", "BESIDE"),
        )
        if answers.strip().upper() == "BESIDE":
            refused_here.add(normalise_label(name))
            log_trace(
                "subdivide.name_refused",
                folder=str(folder),
                axis=settled_axis,
                question=question,
                proposed=[name],
                remembered=False,
            )
            return prompts.Emerging(emerged=False), ()

        signed = await self._llm.structured(
            prompts.build_class_sign(shared=chosen.shared, documents=theirs, language=language),
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
            # The same condition the preview uses: a property already recorded on this
            # folder is a fact, and only a new one is held against the ancestors.
            spent_axes=(
                () if charter is not None and charter.divided else tuple(self._axes_above(folder))
            ),
            depth=len(folder.parts),
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
