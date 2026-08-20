"""SPLIT: dissolving a level that does not earn the guess it costs."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from bismuth.domain.charter import (
    CHARTER_FILENAME,
)
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.maintenance import (
    normalise_label,
    validate_split,
)
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_trace
from bismuth.ports.vault import INBOX
from bismuth.prompts import subdivision as prompts
from bismuth.services.subdivision.naming import (
    _guard_refused,
    _within,
)
from bismuth.services.subdivision.shared import NeedsAFolder

logger = logging.getLogger(__name__)


class DissolvesALevel(NeedsAFolder):
    """SPLIT: dissolving a level that does not earn the guess it costs.

    A mixin: it reads the collaborators and the memories that
    :class:`~bismuth.services.subdivision.service.LibraryMaintenanceService` sets up,
    and is only ever used through it.
    """

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
        # Merge and split are reverse operators, so on unchanged evidence they undo each
        # other (ADR-0018). Comparing the count for equality was not enough: one document
        # arriving between the two answers unlocked the reverse, and the same shelf was
        # built and dissolved fifteen times in one run, twice within three seconds. The
        # folder's own evidence has to double, which is the rule every other schedule
        # here uses and is measured against the folder rather than against a corpus.
        built_at = self._merged.get(str(folder))
        if built_at is not None and self._count_documents(folder, recursive=True) < built_at * 2:
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                reason="this shelf was built here and its evidence has not doubled",
            )
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

        # A level holding one folder and none of its own documents cannot be answering
        # anything: every document under it is under its single child, so the reader pays
        # a guess to reach a list of one. Nothing the model could say would change that,
        # so it is not asked -- 전통시장 및 지역경제 held ten documents through one child
        # and survived a run in which the split question was put 273 times.
        if len(children) == 1 and not here:
            log_trace(
                "subdivide.split_asked",
                folder=str(folder),
                children=1,
                documents=0,
                answer="DISSOLVE",
                reason="one folder below and none of its own, so the level answers nothing",
            )
            report(on_progress, Progress(stage=Stage.DIVIDING, filename=filename, note=str(parent)))
            return self._apply_split(folder, children)

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

    def _remember_dissolved(self, folder: PurePosixPath) -> None:
        """So grouping does not rebuild what splitting just took down.

        The other direction of the same rule. Without it the two operators still trade
        the same shelf, only with grouping paying for the naming call and one closed
        question per folder standing here each time round.
        """
        parent = folder.parent
        self._dissolved[(str(parent), normalise_label(folder.name))] = self._count_documents(
            parent, recursive=True
        )

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
        self._remember_dissolved(folder)
        log_trace(
            "subdivide.split",
            folder=str(folder),
            into=str(parent),
            promoted=[name for name, _, _ in children],
            files=moved,
        )
        return True
