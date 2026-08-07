"""Dividing a folder once its contents show a distinction worth drawing.

The second half of filing (SPEC.md 3.4, ADR-0008). Placement answers "where in the
tree as it stands"; this answers "the tree as it stands is now wrong here". Without
it a first placement is permanent and the documents that arrived first decide the
shape of everything after them.

Reads cards, never documents, and never looks deeper than one level, so the cost of
judging a folder is set by that folder rather than by the size of the archive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.document import DocumentCard, sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_trace
from bismuth.ports.catalog import Catalog
from bismuth.ports.llm import LLM, ModelProfile
from bismuth.ports.vault import INBOX, Vault
from bismuth.prompts import subdivision as prompts
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import read_sidecar_meta
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)

MAX_DEPTH = 8
"""Backstop only. Termination comes from every child being strictly smaller than its
parent; this catches a mistake in that argument before it catches a process."""


@dataclass(frozen=True, slots=True)
class Divided:
    """What dividing one folder did."""

    folder: PurePosixPath
    created: tuple[PurePosixPath, ...] = ()
    moved: int = 0
    renamed_to: str = ""
    basis: str = ""

    @property
    def happened(self) -> bool:
        return bool(self.created)


@dataclass(slots=True)
class _Contents:
    """One folder as the model is shown it: cards, not documents."""

    documents: list[tuple[str, str, PurePosixPath]] = field(default_factory=list)
    """(document_id, one-line description, file path)."""
    children: list[tuple[str, str]] = field(default_factory=list)

    @property
    def lines(self) -> list[tuple[str, str]]:
        return [(document_id, line) for document_id, line, _ in self.documents]

    def path_of(self, document_id: str) -> PurePosixPath | None:
        return next((p for i, _, p in self.documents if i == document_id), None)


class SubdivisionService:
    """Divides a folder when its own contents say it should be divided."""

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
            results.extend(await self.consider(parent, filename=filename, on_progress=on_progress))
            folder = parent
            parent = folder.parent
        return results

    async def consider(
        self,
        folder: PurePosixPath,
        *,
        filename: str = "",
        on_progress: ProgressSink | None = None,
        depth: int = 0,
    ) -> list[Divided]:
        """Divide ``folder`` if it should be, then consider whatever that created.

        Children are strictly smaller than their parent, so the recursion ends.

        ``filename`` is the document whose arrival prompted this, carried only so the
        progress events join that document's run rather than opening one of their own.
        """
        if folder.parts and folder.parts[0] == INBOX.parts[0]:
            return []  # the inbox holds what could not be read; it is not a category

        contents = self._read(folder)
        charter = self._charter(folder)

        if charter is not None and not charter.managed:
            return []  # a human wrote this note; their structure is not ours to redraw

        plan = await self._judge(
            folder, contents, charter, filename=filename, on_progress=on_progress
        )
        if plan is None or not plan.groups:
            return []

        divided = self._apply(folder, contents, plan, charter)
        if not divided.happened:
            return []

        results = [divided]
        if depth >= MAX_DEPTH:
            # Every child is strictly smaller than its parent, so this cannot be reached
            # by a well-behaved division. It is here so that a mistake in that argument
            # costs a log line rather than a process.
            logger.warning("stopped dividing below %s at depth %d", folder or "/", depth)
            return results
        for child in divided.created:
            results.extend(
                await self.consider(
                    child, filename=filename, on_progress=on_progress, depth=depth + 1
                )
            )
        return results

    async def _judge(
        self,
        folder: PurePosixPath,
        contents: _Contents,
        charter: Charter | None,
        *,
        filename: str,
        on_progress: ProgressSink | None,
    ) -> prompts.Division | None:
        """Ask the model. Returns None when there is nothing to ask about."""
        if not contents.documents:
            return None

        purpose = charter.purpose if charter else ""
        # Through the subtree: dividing moves this folder's documents into its children,
        # so a direct count collapses to nothing and the division is never looked at again.
        total = self._vault.count_files(folder, recursive=True)

        # Two different jobs, and only the second may move a document that is already
        # filed. Drawing a new class out of the loose pile is additive and safe to ask
        # often; redrawing a boundary is not, and waits for the evidence to double.
        if charter is not None and charter.divided and charter.due_for_review(total):
            report(
                on_progress,
                Progress(stage=Stage.REVIEWING, filename=filename, note=str(folder) or "/"),
            )
            review = await self._llm.structured(
                prompts.build_review(
                    path=str(folder),
                    purpose=purpose,
                    basis=charter.split_basis,
                    before=charter.split_at_documents,
                    count=total,
                    documents=contents.lines,
                    children=contents.children,
                ),
                schema=prompts.Review,
                profile=ModelProfile.REASONING,
            )
            log_trace(
                "subdivide.review",
                folder=str(folder),
                basis=charter.split_basis,
                before=charter.split_at_documents,
                now=total,
                holds=review.holds,
                reason=review.reason,
            )
            if not review.holds:
                return prompts.Division(
                    divide=True,
                    basis=review.basis or charter.split_basis,
                    groups=review.groups,
                    rename_to=review.rename_to,
                    reason=review.reason,
                )
            # A holding review is still a judgement made at this size, and it has to be
            # recorded as one. Left unwritten, the folder stays past its doubling for
            # ever: it was asked on every ingest from then on -- fourteen times in a row
            # on one run, all of them holding -- and, worse, the answer returned here so
            # the folder was never asked what else had grown in it.
            self._rearm(folder, charter, documents=total)

        if not Charter.due_for_first_look(len(contents.documents)):
            # Said out loud, because otherwise a folder that was never asked and a
            # folder that was asked and declined leave the same trace: none.
            divided = charter is not None and charter.divided
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                documents=len(contents.documents),
                subtree=total,
                reason="not at a power of two",
                next_at=_next_power_of_two(len(contents.documents)),
                divided=divided,
                # Both schedules, because "why did nothing happen here" is usually one
                # of the two and the answer should not need the source to work out.
                review_due_at=(charter.split_at_documents * 2 if divided and charter else None),
            )
            return None

        report(
            on_progress, Progress(stage=Stage.DIVIDING, filename=filename, note=str(folder) or "/")
        )

        # One class at a time, never a partition. Asked to split a heterogeneous pile the
        # model has to put the remainder somewhere, and it names it -- three runs produced
        # `그 밖의 무관한 학술 논문`, `그 밖의 주제`, `기타 주제`, the last one while the
        # prompt banned exactly those words. Nothing here can express "the rest".
        emerging = await self._llm.structured(
            prompts.build_emerging(
                path=str(folder),
                purpose=purpose,
                documents=contents.lines,
                children=contents.children,
            ),
            schema=prompts.Emerging,
            profile=ModelProfile.REASONING,
        )
        log_trace(
            "subdivide.emerging",
            folder=str(folder),
            documents=len(contents.documents),
            subtree=total,
            emerged=emerging.emerged,
            name=emerging.name,
            reason=emerging.reason,
        )
        if not emerging.emerged or not emerging.name.strip():
            return None

        members = await self._llm.structured(
            prompts.build_members(
                path=str(folder),
                purpose=purpose,
                documents=contents.lines,
                children=contents.children,
                name=emerging.name,
                note=emerging.note,
            ),
            schema=prompts.Members,
            profile=ModelProfile.REASONING,
        )
        log_trace(
            "subdivide.members",
            folder=str(folder),
            name=emerging.name,
            claimed=len(members.document_ids),
            of=len(contents.documents),
            reason=members.reason,
        )
        if not members.document_ids:
            return None

        return prompts.Division(
            divide=True,
            basis=f"자란 부류부터 하나씩 꺼냄 — 최근: {emerging.name}",
            groups=[
                prompts.Group(
                    name=emerging.name, note=emerging.note, document_ids=members.document_ids
                )
            ],
            reason=members.reason,
        )

    def _apply(
        self,
        folder: PurePosixPath,
        contents: _Contents,
        plan: prompts.Division,
        charter: Charter | None,
    ) -> Divided:
        """Create the sub-folders, move the documents, write the notes. One entry."""
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        created: list[PurePosixPath] = []
        moved = 0

        for group in plan.groups:
            try:
                name = sanitize_segment(group.name)
            except ValueError:
                logger.warning("division proposed an unusable folder name %r; skipping", group.name)
                continue
            target = folder / name
            if target == folder or self._vault.exists(target):
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

            note = Charter(
                path=target,
                title=name,
                purpose=group.note,
                holds=(),
                answers=(),
            )
            operations.append(
                Operation(
                    kind=OperationKind.WRITE, target=target / CHARTER_FILENAME, note="folder note"
                )
            )
            payloads[target / CHARTER_FILENAME] = note.to_markdown().encode("utf-8")

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
            folder, charter, plan, documents=self._vault.count_files(folder, recursive=True)
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
            renamed_to=plan.rename_to or None,
        )
        logger.info(
            "divided %s into %d folder(s), moved %d document(s)", folder or "/", len(created), moved
        )
        return Divided(
            folder=folder,
            created=tuple(created),
            moved=moved,
            renamed_to=plan.rename_to or "",
            basis=plan.basis,
        )

    def _rearm(self, folder: PurePosixPath, charter: Charter, *, documents: int) -> None:
        """Record that the division was upheld at this size, so the next look waits."""
        held = charter.model_copy(update={"split_at_documents": documents})
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
        title = plan.rename_to or (charter.title if charter else (folder.name or "Vault root"))
        return Charter(
            path=folder,
            title=title,
            purpose=charter.purpose if charter else "",
            holds=charter.holds if charter else (),
            answers=charter.answers if charter else (),
            split_basis=plan.basis or "divided",
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

    def _read(self, folder: PurePosixPath) -> _Contents:
        """The folder as the model will see it: one line per document, one per child."""
        contents = _Contents()
        for path in self._vault.iter_files(folder, recursive=False):
            document_id, card = self._card_of(path)
            if card is None:
                continue
            contents.documents.append((document_id, _describe(card), path))

        for child in self._vault.iter_folders():
            if child.parent != folder or not child.parts:
                continue
            if child.parts[0] == INBOX.parts[0]:
                continue
            note = ""
            try:
                if loaded := self._charters.load(child):
                    note = loaded.purpose
            except BismuthError:
                pass
            contents.children.append((child.name, note))
        return contents

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

    def _charter(self, folder: PurePosixPath) -> Charter | None:
        try:
            return self._charters.load(folder)
        except BismuthError as exc:
            logger.warning("unreadable folder note at %s: %s", folder or "/", exc)
            return None


def _next_power_of_two(documents: int) -> int:
    """The size this folder will next be asked at. For the trace, not for a decision."""
    size = 2
    while size <= documents:
        size *= 2
    return size


def _describe(card: DocumentCard) -> str:
    """One line per document. Enough to group by, short enough that a folder fits."""
    topics = ", ".join(card.topics[:4])
    return f"{card.title} | {card.doc_type}" + (f" | {topics}" if topics else "")
