"""Redrawing the whole collection, in one transaction or not at all.

Every other operation in this program is local: a folder is asked about itself, with
the documents it holds in front of it. That is what makes them cheap and what makes
them blind. 금융 at the root and 금융업 six levels down are the same subject in two
places, and no folder-local question can ever be shown both.

So this pass stands outside every folder. It is the only place a wrong top-level
boundary can be corrected, now that redrawing one from inside a folder is retired
(ADR-0018, docs/spec/maintenance.md 4-5).

Three properties hold it to a size the collection cannot outgrow:

**The design is one call.** It reads the subject vocabulary the cards already carry --
arithmetic over words we hold, no model call to produce -- so it costs the same for
three hundred documents as for thirty thousand.

**Assignment is per folder, not per document.** A folder moves whole, so a subtree of
four hundred documents is one closed question, the same as a folder of two. Only the
documents still loose at the root are asked individually, because nothing else can
speak for them.

**Nothing is half done.** Academic libraries that converted from Dewey to LC in the
1960s ran out of budget mid-project and were left with one collection in two schemes.
Every move here is in a single journal entry: it applies completely or the vault is
exactly as it was.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.document import sidecar_name
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.maintenance import normalise_label, validate_names
from bismuth.domain.paths import sanitize_segment
from bismuth.logging_setup import log_context, log_trace
from bismuth.ports.llm import LLM
from bismuth.ports.vault import INBOX, Vault
from bismuth.prompts import redesign as prompts
from bismuth.services.charters import CharterService
from bismuth.services.transactor import Transactor

MIN_CLASSES = 3
"""Fewer than three answers is not a division of a collection, it is a rename."""


@dataclass(frozen=True, slots=True)
class Redesign:
    """What the pass decided, whether or not it was applied."""

    question: str = ""
    axis: str = ""
    classes: tuple[tuple[str, str], ...] = ()
    """(name, sign) for each new top-level folder."""
    moved_folders: tuple[str, ...] = ()
    moved_documents: int = 0
    unsound: tuple[str, ...] = ()
    """Folders the design judged do not say what a reader would find inside them."""
    refused: str = ""
    """Why nothing was applied, when nothing was."""

    @property
    def applied(self) -> bool:
        return bool(self.classes) and not self.refused


@dataclass
class _Standing:
    """The top of the vault as it is now."""

    folders: list[tuple[str, str, int]] = field(default_factory=list)
    """(name, sign, documents through the subtree)."""
    documents: list[tuple[PurePosixPath, str]] = field(default_factory=list)
    """Loose at the root: (path, one-line description)."""
    vocabulary: list[str] = field(default_factory=list)
    language: str = ""


class RedesignService:
    """The whole-collection pass. Asked for explicitly; never on the filing path."""

    def __init__(
        self,
        *,
        vault: Vault,
        charters: CharterService,
        transactor: Transactor,
        llm: LLM,
        read_folder: Callable[..., Any],
    ) -> None:
        self._vault = vault
        self._charters = charters
        self._transactor = transactor
        self._llm = llm
        # The maintenance service already knows how to read a folder as cards rather
        # than as bytes, and that reading is not this pass's business to reinvent.
        self._read = read_folder

    async def redesign(self) -> Redesign:
        """Draw a new top level and move everything under it. One entry, or none."""
        with log_context(stage="redesign"):
            standing = self._standing()
            if len(standing.folders) + len(standing.documents) < MIN_CLASSES:
                return Redesign(refused="there is not enough standing here to redraw")

            design = await self._llm.structured(
                prompts.build_design(
                    vocabulary=standing.vocabulary,
                    folders=standing.folders,
                    language=standing.language,
                ),
                schema=prompts.Design,
            )
            log_trace(
                "redesign.designed",
                axis=design.axis,
                question=design.question,
                classes=[item.name for item in design.classes],
                unsound=design.unsound,
                folders=len(standing.folders),
                documents=len(standing.documents),
                vocabulary=len(standing.vocabulary),
            )
            if refusal := self._refuse(design, standing):
                log_trace("redesign.refused", reason=refusal, axis=design.axis)
                return Redesign(refused=refusal, unsound=tuple(design.unsound))

            classes = [(sanitize_segment(item.name), item.sign.strip()) for item in design.classes]
            placed = await self._assign(classes, standing)
            if not placed:
                return Redesign(
                    question=design.question,
                    axis=design.axis,
                    classes=tuple(classes),
                    unsound=tuple(design.unsound),
                    refused="nothing found a place under the new top level",
                )
            return self._apply(design, classes, placed, standing)

    # -- reading -----------------------------------------------------------------

    def _standing(self) -> _Standing:
        """The root as it is: its folders, its loose documents, and its vocabulary."""
        standing = _Standing()
        contents = self._read(PurePosixPath())
        for name, note in contents.children:
            if name == INBOX.parts[0]:
                continue
            folder = PurePosixPath(name)
            standing.folders.append((name, note, self._count(folder)))
        for _, description, path in contents.documents:
            standing.documents.append((path, description))

        # Through the subtree: the top of the tree is drawn from what the collection is
        # about, not from whatever happens to be loose at the root today.
        whole = self._read(PurePosixPath(), recursive=True)
        counted: dict[str, int] = {}
        for _, topics in whole.topics:
            for topic in topics:
                if cleaned := topic.strip():
                    counted[cleaned] = counted.get(cleaned, 0) + 1
        standing.vocabulary = [
            topic for topic, _ in sorted(counted.items(), key=lambda item: (-item[1], item[0]))
        ][:120]
        standing.language = whole.language
        return standing

    def _count(self, folder: PurePosixPath) -> int:
        return sum(1 for _ in self._vault.iter_files(folder, recursive=True))

    # -- deciding ----------------------------------------------------------------

    def _refuse(self, design: prompts.Design, standing: _Standing) -> str:
        """The contracts a design has to meet before anything is moved.

        Names first, because they are string comparisons and the assignment loop that
        follows costs one model call per folder.
        """
        if len(design.classes) < MIN_CLASSES:
            return "fewer than three answers is a rename, not a division"
        names = tuple(item.name for item in design.classes)
        validation = validate_names(axis=design.axis, axis_question=design.question, names=names)
        if not validation.accepted:
            return "; ".join(problem.value for problem in validation.problems)
        # A new top level whose names are the old ones has redrawn nothing, and would
        # spend the whole assignment loop discovering that.
        standing_keys = {normalise_label(name) for name, _, _ in standing.folders}
        if standing_keys and {normalise_label(name) for name in names} <= standing_keys:
            return "the new top level is the folders that already stand here"
        return ""

    async def _assign(
        self, classes: list[tuple[str, str]], standing: _Standing
    ) -> dict[str, list[PurePosixPath]]:
        """One closed choice per folder, and one per document loose at the root."""
        offered = [
            (f"C{index:03d}", name, sign or name)
            for index, (name, sign) in enumerate(classes, start=1)
        ]
        by_handle = {handle: name for handle, name, _ in offered}
        placed: dict[str, list[PurePosixPath]] = {name: [] for name, _ in classes}
        taken = {normalise_label(name) for name, _ in classes}

        for name, note, count in standing.folders:
            # A folder whose name the new top level also uses is that folder, already in
            # its place. Moving it inside itself is the one answer that cannot be right.
            if normalise_label(name) in taken:
                continue
            answer = await self._llm.choose(
                prompts.build_assignment(subject=name, note=note, count=count, classes=offered),
                choices=(*by_handle, "STAY"),
            )
            if chosen := by_handle.get(answer.strip().upper()):
                placed[chosen].append(PurePosixPath(name))

        for path, description in standing.documents:
            answer = await self._llm.choose(
                prompts.build_assignment(subject=description, note="", count=0, classes=offered),
                choices=(*by_handle, "STAY"),
            )
            if chosen := by_handle.get(answer.strip().upper()):
                placed[chosen].append(path)

        return {name: items for name, items in placed.items() if items}

    # -- applying ----------------------------------------------------------------

    def _apply(
        self,
        design: prompts.Design,
        classes: list[tuple[str, str]],
        placed: dict[str, list[PurePosixPath]],
        standing: _Standing,
    ) -> Redesign:
        """Every move in one journal entry, so a stopped redesign cannot exist."""
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        folder_names = {name for name, _, _ in standing.folders}
        moved_folders: list[str] = []
        moved_documents = 0

        for name, sign in classes:
            target = PurePosixPath(name)
            if name not in placed:
                continue
            if not self._vault.exists(target):
                operations.append(Operation(kind=OperationKind.MKDIR, target=target))
                shelf = Charter(
                    path=target,
                    title=name,
                    purpose=sign or name,
                    split_basis="",
                    split_question="",
                    holds=(),
                    answers=(),
                )
                operations.append(
                    Operation(
                        kind=OperationKind.WRITE,
                        target=target / CHARTER_FILENAME,
                        note="folder note",
                    )
                )
                payloads[target / CHARTER_FILENAME] = shelf.to_markdown().encode("utf-8")

            for item in placed[name]:
                if str(item) in folder_names:
                    operations.extend(self._move_folder(item, target))
                    moved_folders.append(str(item))
                else:
                    operations.extend(self._move_document(item, target))
                    moved_documents += 1

        root = self._charters.load(PurePosixPath())
        note = (root or Charter(path=PurePosixPath(), title="/", purpose="")).model_copy(
            update={
                "split_basis": design.axis.strip(),
                "split_question": design.question.strip(),
                "split_at_documents": sum(count for _, _, count in standing.folders),
            }
        )
        operations.append(
            Operation(
                kind=OperationKind.WRITE,
                target=PurePosixPath(CHARTER_FILENAME),
                note="folder note",
            )
        )
        payloads[PurePosixPath(CHARTER_FILENAME)] = note.to_markdown().encode("utf-8")

        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=(
                    f"redesign the top level on {design.axis or 'a new property'}: "
                    f"{len(moved_folders)} folder(s), {moved_documents} document(s)"
                ),
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        log_trace(
            "redesign.applied",
            axis=design.axis,
            classes=[name for name, _ in classes],
            folders=moved_folders,
            documents=moved_documents,
        )
        return Redesign(
            question=design.question,
            axis=design.axis,
            classes=tuple(classes),
            moved_folders=tuple(moved_folders),
            moved_documents=moved_documents,
            unsound=tuple(design.unsound),
        )

    def _move_folder(self, source: PurePosixPath, target: PurePosixPath) -> list[Operation]:
        """Move a folder whole: no document changes the folder it is in."""
        operations: list[Operation] = []
        subtree = sorted(
            (f for f in self._vault.iter_folders() if f == source or f.is_relative_to(source)),
            key=lambda f: len(f.parts),
        )
        emptied: list[PurePosixPath] = []
        for sub in subtree:
            destination = target / sub.relative_to(source.parent)
            operations.append(Operation(kind=OperationKind.MKDIR, target=destination))
            for path in sorted(self._vault.iter_files(sub, recursive=False)):
                operations.extend(self._move_document(path, destination, note="redesign"))
            folder_note = sub / CHARTER_FILENAME
            if self._vault.exists(folder_note):
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=folder_note,
                        target=destination / CHARTER_FILENAME,
                        note="folder note",
                    )
                )
            emptied.append(sub)
        operations.extend(
            Operation(kind=OperationKind.RMDIR, target=path) for path in reversed(emptied)
        )
        return operations

    def _move_document(
        self, path: PurePosixPath, target: PurePosixPath, *, note: str = "redesign"
    ) -> list[Operation]:
        operations = [
            Operation(kind=OperationKind.MOVE, source=path, target=target / path.name, note=note)
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
