"""The folder maintenance service: what may be asked here, and what happened."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import TypeVar

from bismuth.domain.charter import (
    CHARTER_FILENAME,
    Charter,
    routing_purpose,
    routing_sign,
)
from bismuth.domain.document import DocumentCard, sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Operation, OperationKind
from bismuth.domain.maintenance import (
    FolderShape,
    Operator,
    legal_operators,
    normalise_label,
)
from bismuth.domain.progress import ProgressSink
from bismuth.logging_setup import log_context, log_trace
from bismuth.ports.catalog import Catalog
from bismuth.ports.llm import LLM
from bismuth.ports.vault import INBOX, Vault
from bismuth.prompts import subdivision as prompts
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import read_sidecar_meta
from bismuth.services.subdivision.emerging import DrawsAClass
from bismuth.services.subdivision.grouping import StandsFoldersTogether
from bismuth.services.subdivision.naming import (
    _in_inbox,
    _writing_system,
)
from bismuth.services.subdivision.reading import (
    Divided,
    _Contents,
    _describe,
)
from bismuth.services.subdivision.routing import RoutesToAStandingSign
from bismuth.services.subdivision.splitting import DissolvesALevel
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)

PacketT = TypeVar("PacketT")


class LibraryMaintenanceService(
    DrawsAClass, RoutesToAStandingSign, StandsFoldersTogether, DissolvesALevel
):
    """Maintains the classification tree as evidence arrives.

    Placement shelves one document against the tree that exists.  This service owns
    changes to that tree: adding a class and reviewing or replacing an old boundary.
    Keeping the use cases separate makes a maintenance failure independent from a
    successfully filed document."""

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
        self._barren: dict[tuple[str, str], int] = {}
        self._merged: dict[str, int] = {}
        self._not_an_answer: dict[tuple[str, str], set[str]] = {}
        """Names the check turned down here, keyed by the question they failed to answer.

        A name refused under one question can be the right name under another, so the
        question is part of the key and a redrawn boundary forgets everything.
        """
        self._declined: dict[tuple[str, str], set[str]] = {}
        """Documents that answered STAY to a folder's signs, keyed by those signs: a new
        sign invalidates the memory, and nothing else needs to."""
        self._dissolved: dict[tuple[str, str], int] = {}

    def _asked_before(self, folder: PurePosixPath, name: str, *, documents: int) -> bool:
        """Whether this name already bought nothing here, on evidence barely different.

        A proposal that shelved no document is not refused anywhere: the chain returns
        None and nothing is written down, so the next arrival proposes it again from the
        same pile. Measured on 300 documents: 중소벤처기업부 was proposed 55 times in one
        folder, asked 5,125 membership questions and shelved nothing, once. Three names
        account for 9,606 of the run's 10,512 membership questions and none of them
        shelved a single document.

        Doubling, so it is the folder's own history that unlocks the question rather than
        a number tuned to a corpus, and it is the same rule the boundary used to be held
        to. Kept for the life of the process only -- a restart re-asks once, which costs
        one loop and cannot compound.
        """
        seen = self._barren.get((str(folder), normalise_label(name)))
        return seen is not None and documents < seen * 2

    def _bought_nothing(self, folder: PurePosixPath, name: str, *, documents: int) -> None:
        self._barren[(str(folder), normalise_label(name))] = documents

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

        # Which of the four operators could be applied here at all, counted from the
        # filesystem before anything is asked (ADR-0018, docs/spec/maintenance.md 2). An
        # operator whose contract could only refuse the answer is not offered, so there is
        # nothing to refuse: one 300-document run asked 52 folders holding three documents
        # or fewer to divide, and threw away 36 answers for taking the whole pile -- six of
        # them from folders holding two documents, where no legal answer existed.
        legal = legal_operators(self._shape_of(folder, contents))

        # Before asking what could come out of this folder, ask whether the folder should
        # be here at all. It runs first, because the folders that most need the question
        # are the ones that have stopped dividing -- grouping sits after a successful
        # division and so can only ever widen a tree that is already moving. If the level
        # goes, there is nothing left here to divide.
        if Operator.SPLIT in legal:
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
                may_create=Operator.CREATE in legal,
            )
            if plan is None or not plan.groups:
                return []

            divided = (
                self._route_existing(folder, contents, plan, charter)
                if plan.reuse_existing
                else self._apply(folder, contents, plan, charter)
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
        if not divided.routed and Operator.MERGE in legal_operators(
            self._shape_of(folder, self._read(folder))
        ):
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

    def _shape_of(self, folder: PurePosixPath, contents: _Contents) -> FolderShape:
        """What the enumeration needs, counted from the filesystem and nothing else."""
        parent_children = (
            [name for name, _ in self._read(folder.parent).children if name != folder.name]
            if folder.parts
            else []
        )
        return FolderShape(
            loose_documents=len(contents.documents),
            depth=len(folder.parts),
            children=tuple(name for name, _ in contents.children),
            ancestor_names=folder.parts,
            siblings=tuple(parent_children),
            is_root=not folder.parts,
            subtree_depth=self._subtree_depth(folder),
        )

    def _subtree_depth(self, folder: PurePosixPath) -> int:
        """How many levels of folder stand below this one; ``0`` for a leaf."""
        children = [name for name, _ in self._read(folder).children if name != INBOX.parts[0]]
        if not children:
            return 0
        return 1 + max(self._subtree_depth(folder / name) for name in children)

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
            # Not ours to clear. The whole-collection pass records when it last drew the
            # top of the tree here, and this note is rewritten on every root division --
            # eighteen times in one run, which is how that record went missing.
            redrawn_at_documents=charter.redrawn_at_documents if charter else 0,
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

    def read(self, folder: PurePosixPath, *, recursive: bool = False) -> _Contents:
        """The folder as the model sees it: cards, not bytes.

        Public because the whole-collection pass reads a folder the same way, and a
        second answer to "what is in this folder" would drift from this one.
        """
        return self._read(folder, recursive=recursive)

    def _names_in_use(self, *, except_under: PurePosixPath | None = None) -> frozenset[str]:
        """Every folder name standing in this vault, normalised.

        A name in two branches is the one shape an agent cannot recover from: it opens the
        first, judges the page, and never learns the second exists. Measured -- 금융 및
        공정거래 stood at the root and again inside 산업 및 경제 분야 규제, holding 110
        documents about finance between them.

        ``except_under`` leaves out a subtree that is about to move or be replaced, so a
        folder is not held to be a duplicate of itself.
        """
        return frozenset(
            normalise_label(folder.name)
            for folder in self._vault.iter_folders()
            if folder.parts
            and folder.parts[0] != INBOX.parts[0]
            and not (except_under is not None and folder.is_relative_to(except_under))
        )

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
            contents.topics.append((document_id, tuple(card.topics)))
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


# Compatibility for embedders that used the alpha API. New code should name the role,
# not the one operation the first implementation happened to support.
SubdivisionService = LibraryMaintenanceService
