"""MERGE: standing folders that already exist together under one broader name."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from bismuth.domain.charter import (
    CHARTER_FILENAME,
    Charter,
)
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.maintenance import (
    normalise_label,
    validate_grouping,
)
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_trace
from bismuth.ports.vault import INBOX
from bismuth.prompts import subdivision as prompts
from bismuth.services.subdivision.naming import (
    _sign,
    _within,
)
from bismuth.services.subdivision.shared import NeedsAFolder

logger = logging.getLogger(__name__)


class StandsFoldersTogether(NeedsAFolder):
    """MERGE: standing folders that already exist together under one broader name.

    A mixin: it reads the collaborators and the memories that
    :class:`~bismuth.services.subdivision.service.LibraryMaintenanceService` sets up,
    and is only ever used through it.
    """

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

        here = self._count_documents(folder, recursive=True)
        taken_down = self._dissolved.get((str(folder), normalise_label(proposal.name)))
        if taken_down is not None and here < taken_down * 2:
            log_trace(
                "subdivide.grouping_rejected",
                folder=str(folder),
                reason="this shelf was dissolved here and the evidence has not doubled",
                proposed=proposal.name,
            )
            return False

        # Naming a folder that already stands here is not a collision, it is the cheaper
        # answer: these folders move inside that one and no level is created. Asked for
        # five times in one run and refused five times, because the name it wanted was
        # held by the folder it wanted to move them into.
        standing = {normalise_label(name): name for name, _, _ in children}
        into = standing.get(normalise_label(proposal.name))
        # A folder cannot stand inside itself, so it is not offered the question.
        asked = [child for child in children if child[0] != into]

        members: list[tuple[str, str, int]] = []
        for child in asked:
            answer = await self._llm.choose(
                prompts.build_grouping_member(
                    path=str(folder), name=into or proposal.name, sign=proposal.sign, child=child
                ),
                choices=("SHELF", "STAY"),
            )
            if answer.strip().upper() == "SHELF":
                members.append(child)

        if not members:
            return False

        # The one operator that invents a name without choosing a property, so nothing the
        # axis check refuses ever reached it. Unchecked, it put 283 of 300 documents behind
        # one folder named after what they are made of.
        #
        # Asked here rather than before the membership loop, which is where it used to sit
        # to save one call per folder standing beside the shelf. It was answering about a
        # name with nothing under it, and it passed 중소기업 지원 관련 법률 -- a name that
        # says what its contents are, which is the one thing it exists to refuse. Shown the
        # eight folders that would actually move, the question is about something.
        verdict = await self._llm.choose(
            prompts.build_shelf_check(
                path=str(folder),
                name=into or proposal.name,
                sign=proposal.sign,
                moving=[name for name, _, _ in members],
                staying=[name for name, _, _ in children if name not in {m for m, _, _ in members}],
            ),
            choices=("CLASS", "CONTAINER"),
        )
        if verdict.strip().upper() == "CONTAINER":
            log_trace(
                "subdivide.grouping_rejected",
                folder=str(folder),
                reason="the broader name says what the documents are, not what they are about",
                proposed=into or proposal.name,
                members=[name for name, _, _ in members],
            )
            return False

        # A shelf that already stands here was named for what it holds, and nothing has
        # asked whether it also answers for what is about to move inside it. Left unasked,
        # 과학기술 연구개발 및 기관 -- 42 documents of research law -- was moved under
        # 중앙행정기관 조직 및 직제, whose name then answered for a fifth of its own
        # contents. Only on this path: a new shelf is named from its members, so its name
        # cannot fail to cover them, while an existing one was named before they existed.
        if into is not None:
            standing_note = next((note for name, note, _ in children if name == into), "")
            for name, note, _ in members:
                answer = await self._llm.choose(
                    prompts.build_covers_check(
                        shelf=into,
                        note=standing_note,
                        incoming=name,
                        incoming_note=note,
                    ),
                    choices=("COVERS", "WIDER"),
                )
                if answer.strip().upper() != "COVERS":
                    log_trace(
                        "subdivide.grouping_rejected",
                        folder=str(folder),
                        reason="the folder standing here does not answer for what would move inside it",
                        proposed=into,
                        members=[name],
                    )
                    return False

        validation = validate_grouping(
            name=into or proposal.name,
            axis=charter.split_basis,
            members=tuple(name for name, _, _ in members),
            siblings=tuple(name for name, _, _ in children),
            ancestor_names=folder.parts,
            into_existing=into is not None,
            taken_anywhere=self._names_in_use(),
            depth=len(folder.parts),
            member_depths=tuple(self._subtree_depth(folder / name) + 1 for name, _, _ in members),
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
            Progress(
                stage=Stage.DIVIDING, filename=filename, note=str(folder / (into or proposal.name))
            ),
        )
        return self._apply_grouping(folder, charter, proposal, members, into=into)

    def _apply_grouping(
        self,
        folder: PurePosixPath,
        charter: Charter,
        proposal: prompts.Grouping,
        members: list[tuple[str, str, int]],
        *,
        into: str | None = None,
    ) -> bool:
        """Move whole sub-folders onto one shelf, in a single undoable batch.

        ``into`` names a folder that is already standing here, in which case nothing is
        created: the folders move inside it and it keeps its own name, note and axis.
        """
        try:
            name = into if into is not None else sanitize_segment(proposal.name)
        except ValueError:
            log_trace(
                "subdivide.grouping_rejected",
                folder=str(folder),
                reason="unusable path segment",
                proposed=proposal.name,
            )
            return False
        target = folder / name
        if into is None and self._vault.exists(target):
            return False
        if into is not None and not self._vault.exists(target):
            return False

        operations: list[Operation] = []
        if into is None:
            operations.append(Operation(kind=OperationKind.MKDIR, target=target))
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
        #
        # A folder that was already standing here keeps the note it has. It was written
        # about its own documents and is still true of them; the folders arriving beside
        # them do not make it false, and rewriting it here would be a boundary redrawn
        # from inside a folder, which is the one thing this service no longer does.
        if into is None:
            shelf = Charter(
                path=target,
                title=name,
                purpose=_sign(
                    proposal.sign, axis=charter.split_basis, class_name=name, folder=folder
                ),
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
                    f"({moved} file(s))" + (" (already standing)" if into is not None else "")
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
            into_existing=into is not None,
        )
        self._merged[str(target)] = self._count_documents(target, recursive=True)
        return True
