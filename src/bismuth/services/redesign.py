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

Nobody asks for it. The product is that a person uploads documents and nothing else
(SPEC.md 5), so a correction pass with a button and no schedule is not one. :meth:`due`
is that schedule.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.document import sidecar_name
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.maintenance import normalise_label, restates, validate_names
from bismuth.domain.paths import sanitize_segment
from bismuth.logging_setup import log_context, log_trace
from bismuth.ports.llm import LLM
from bismuth.ports.vault import INBOX, Vault
from bismuth.prompts import redesign as prompts
from bismuth.prompts import subdivision as subdivision_prompts
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

    folders: list[tuple[str, str, int, int]] = field(default_factory=list)
    """(name, sign, documents through the subtree, documents loose in it).

    The second count is what says whether the folder is working. A folder of 61 that has
    filed all 61 and a folder of 61 with 31 sitting directly in it are the same number and
    opposite states, and the pass was shown only the number.
    """
    documents: list[tuple[PurePosixPath, str]] = field(default_factory=list)
    """Loose at the root: (path, one-line description)."""
    vocabulary: list[str] = field(default_factory=list)
    language: str = ""


def _says_the_same(one: str, other: str) -> bool:
    """Whether two names would leave a reader with the same expectation.

    ``restates`` deliberately answers False for two names that are equal, because a
    descendant repeating its ancestor is the case it was written for. Here equality is
    the strongest form of the thing being asked about: 행정조직 was drawn beside
    행정·조직 and both survived, since the two differ only by punctuation.
    """
    return (
        normalise_label(one) == normalise_label(other)
        or restates(one, other)
        or restates(other, one)
    )


class RedesignService:
    """The whole-collection pass.

    :meth:`due` puts it on the same self-relative schedule as everything else here, and
    ingest asks that question on every arrival. It answers yes on a handful of them.
    """

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
        self._looked_at = 0
        """Documents held when this pass last reached a decision, whatever it decided."""
        # The maintenance service already knows how to read a folder as cards rather
        # than as bytes, and that reading is not this pass's business to reinvent.
        self._read = read_folder

    def due(self) -> bool:
        """Whether the top of the tree is worth drawing again.

        Measured against this pass's own record, not against ``split_at_documents``. That
        field belongs to the incremental path, which rewrites it every time the root
        divides -- eighteen times in one 300-document run, never more than 37 documents
        apart -- so a schedule hung on it can never reach a doubling. Replayed against
        that run, this method answered no on all three hundred arrivals.

        The first time is different from the rest. Until this pass has drawn anything,
        there is no count of its own to double, and the number it would have to borrow is
        the one that keeps moving. So the first draw waits for the root to have enough
        standing on it to be worth drawing at all, and every draw after that waits for the
        collection to double -- self-relative, and measured against its own history.

        Drawing early is the point rather than a cost. The property a root is divided on
        is otherwise chosen by :func:`build_axis` from one group's sentence, over as few
        as two documents; this pass chooses it from the whole collection's vocabulary.

        Scheduling, not judgement: asking late costs a late fix, never a wrong tree
        (SPEC.md 6.1).
        """
        here = self._count(PurePosixPath())
        root = self._charters.load(PurePosixPath())
        redrawn = root.redrawn_at_documents if root is not None else 0
        standing = sum(
            1
            for child in self._vault.iter_folders()
            if child.parent == PurePosixPath() and child.parts and child.parts[0] != INBOX.parts[0]
        )
        # A look that moved nothing is still a look. Kept for the life of the process
        # only: a restart looks once more, which costs one call and cannot compound.
        if self._looked_at > 0 and here < self._looked_at * 2:
            answer, why = False, "looked at this size already"
        elif redrawn > 0:
            answer = here >= redrawn * 2
            why = "the collection has doubled" if answer else "not doubled since it was drawn"
        else:
            answer = standing >= MIN_CLASSES
            why = "never drawn and a top level exists" if answer else "no top level to redraw"
        # Traced on every arrival because a schedule that silently answers no is
        # indistinguishable from one that is not wired up: this fired once where a replay
        # of the same run says it should have fired five times, and nothing said why.
        log_trace(
            "redesign.due",
            due=answer,
            why=why,
            documents=here,
            looked_at=self._looked_at,
            redrawn_at=redrawn,
            standing=standing,
        )
        return answer

    async def redesign(self) -> Redesign:
        """Draw a new top level and move everything under it. One entry, or none."""
        with log_context(stage="redesign"):
            standing = self._standing()
            if len(standing.folders) + len(standing.documents) < MIN_CLASSES:
                return Redesign(refused="there is not enough standing here to redraw")

            # Whatever it decides, it has now looked. Without this a decision that moved
            # nothing left the schedule true and the pass ran again on the very next
            # arrival: 160 attempts in one 300-document run, 155 of which changed nothing.
            self._looked_at = self._count(PurePosixPath())

            design = await self._design(standing)
            if design is None:
                return Redesign(refused="no property this collection could be drawn on")
            if not design.classes:
                # Not a refusal. The one answer every other question in this program can
                # give and this one could not, which is why it kept answering with the
                # folders that already stood there and being turned down for it.
                log_trace("redesign.left_alone", axis=design.axis, folders=len(standing.folders))
                return Redesign(
                    question=design.question,
                    axis=design.axis,
                    unsound=tuple(design.unsound),
                    refused="the top level standing here is already a good one",
                )
            if refusal := self._refuse(design, standing):
                log_trace("redesign.refused", reason=refusal, axis=design.axis)
                return Redesign(refused=refusal, unsound=tuple(design.unsound))

            classes = self._buildable(
                [(sanitize_segment(item.name), item.sign.strip()) for item in design.classes],
                standing,
            )
            if len(classes) < MIN_CLASSES:
                return Redesign(
                    question=design.question,
                    axis=design.axis,
                    unsound=tuple(design.unsound),
                    refused="too few of these answers could be built where they were drawn",
                )
            placed = await self._assign(classes, standing)
            placed = await self._only_classes(placed, classes)
            if not placed:
                return Redesign(
                    question=design.question,
                    axis=design.axis,
                    classes=tuple(classes),
                    unsound=tuple(design.unsound),
                    refused="nothing found a place under the new top level",
                )
            return self._apply(design, classes, placed, standing)

    async def _only_classes(
        self,
        placed: dict[str, list[PurePosixPath]],
        classes: list[tuple[str, str]],
    ) -> dict[str, list[PurePosixPath]]:
        """Drop the classes that name what their contents are rather than what they are about.

        The same closed question a new shelf is held to, and the pass that invents the
        whole top level was the one operator it was never asked. 특수 분야 지원 collected
        여성·장애인기업 and 중대재해 -- two unrelated things under a name meaning assorted
        support -- and split dissolved it six minutes later, having paid to move eight
        files twice.

        Asked once per class that actually collected something, so a design of nine costs
        as many calls as it has live classes and none for the ones nothing chose.
        """
        signs = dict(classes)
        for name, items in list(placed.items()):
            verdict = await self._llm.choose(
                subdivision_prompts.build_shelf_check(
                    path="",
                    name=name,
                    sign=signs.get(name, ""),
                    moving=[item.name for item in items],
                    staying=[other for other in placed if other != name],
                ),
                choices=("CLASS", "CONTAINER"),
            )
            if verdict.strip().upper() == "CONTAINER":
                log_trace(
                    "redesign.dropped",
                    klass=name,
                    reason="the name says what its contents are, not what they are about",
                    members=[item.name for item in items],
                )
                placed.pop(name)
        return placed

    async def _design(self, standing: _Standing) -> prompts.Design | None:
        """The one call that decides the top of the tree, held to the same property
        contract as every division inside a folder.

        Asked twice at most. The first real redesign came back with 행정부처 관할 -- who
        administers the document -- and drew good subject names under a question that
        would have made every later root class a ministry. Refusing outright would leave
        the collection undrawn until it doubles again, so the second ask is told what was
        turned down.
        """
        refused: list[str] = []
        for attempt in range(2):
            design = await self._llm.structured(
                prompts.build_design(
                    vocabulary=standing.vocabulary,
                    folders=standing.folders,
                    refused=refused,
                    language=standing.language,
                ),
                schema=prompts.Design,
            )
            log_trace(
                "redesign.designed",
                attempt=attempt + 1,
                axis=design.axis,
                question=design.question,
                classes=[item.name for item in design.classes],
                unsound=design.unsound,
                folders=len(standing.folders),
                documents=len(standing.documents),
                vocabulary=len(standing.vocabulary),
            )
            if not design.axis.strip() or not design.classes:
                return design
            verdict = await self._llm.choose(
                subdivision_prompts.build_axis_check(
                    path="",
                    axis=design.axis,
                    axis_question=design.question,
                    name=design.classes[0].name,
                    spent=[],
                ),
                choices=("FAILS", "HOLDS"),
            )
            if verdict.strip().upper() != "FAILS":
                return design
            log_trace("redesign.axis_refused", attempt=attempt + 1, axis=design.axis)
            refused.append(design.axis.strip())
        return None

    # -- reading -----------------------------------------------------------------

    def _standing(self) -> _Standing:
        """The tree as it is: every folder, the root's loose documents, and the vocabulary.

        Every folder, not only the root's children. A subject buried one level down could
        otherwise never be pulled up beside the one it belongs with -- which is the whole
        reason this pass exists. Measured: a redesign drew 금융·투자 at the root while
        금융 및 보험 sat inside 산업·경제 규제, and 85 documents about finance ended in two
        places because the second was never a candidate.

        Still one question per folder, so the pass stays bounded by folders rather than
        by documents.
        """
        standing = _Standing()
        for folder in sorted(self._vault.iter_folders(), key=lambda f: len(f.parts)):
            if not folder.parts or folder.parts[0] == INBOX.parts[0]:
                continue
            note = ""
            if (loaded := self._charters.load(folder)) is not None:
                note = loaded.purpose
            standing.folders.append((str(folder), note, self._count(folder), self._loose(folder)))
        contents = self._read(PurePosixPath())
        for _, description, path in contents.documents:
            standing.documents.append((path, description))

        # Through the subtree: the top of the tree is drawn from what the collection is
        # about, not from whatever happens to be loose at the root today.
        whole = self._read(PurePosixPath(), recursive=True)
        counted: dict[str, int] = {}
        homes: dict[str, set[str]] = {}
        for document_id, topics in whole.topics:
            path = whole.path_of(document_id)
            home = path.parts[0] if path is not None and path.parts else ""
            for topic in topics:
                if cleaned := topic.strip():
                    counted[cleaned] = counted.get(cleaned, 0) + 1
                    homes.setdefault(cleaned, set()).add(home)
        # Fewest homes first, and only then the most common. A subject that appears under
        # every folder distinguishes nothing, however often it is written down, and this
        # collection's most common subjects were exactly those: 과태료, 벌칙, 대통령령 and
        # 규제 재검토 each appeared under all six roots, while 전통시장, 온누리상품권 and
        # 연구개발비 each appeared under one. Ranked by count, twelve of the top fifteen
        # were boilerplate -- the model was told not to divide on what documents ARE and
        # then handed a list sorted by it.
        #
        # Self-relative and it degrades on its own: while one folder stands, every subject
        # has one home and this is the old ranking exactly.
        standing.vocabulary = [
            topic
            for topic, _ in sorted(
                counted.items(), key=lambda item: (len(homes[item[0]]), -item[1], item[0])
            )
        ][:120]
        standing.language = whole.language
        return standing

    def _loose(self, folder: PurePosixPath) -> int:
        """Documents sitting in this folder rather than behind one of its children."""
        return sum(
            1
            for path in self._vault.iter_files(folder, recursive=False)
            if not path.parts or path.parts[0] != INBOX.parts[0]
        )

    def _count(self, folder: PurePosixPath) -> int:
        """Documents the collection holds, which is not the same as files in the vault.

        The inbox holds what has been uploaded and not yet filed, and a bulk upload puts
        everything there at once. Counting it made the schedule see a complete collection
        before a single document had been placed: it looked once, wrote 300 down, and its
        own doubling rule then blocked the remaining 287 arrivals.
        """
        return sum(
            1
            for path in self._vault.iter_files(folder, recursive=True)
            if not path.parts or path.parts[0] != INBOX.parts[0]
        )

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
        # Nothing here about names that repeat a folder standing at the root. A class
        # broader than a folder it will hold is the whole point of the pass -- 연구개발
        # 및 과학기술 over 연구개발 was refused 55 times for that, and 소비자 over
        # 소비자 보호 28 more. Whether a class turned out to be a pass-through is a fact
        # about what it collected, so it is decided after the assignment, not here.
        return ""

    def _buildable(
        self, classes: list[tuple[str, str]], standing: _Standing
    ) -> list[tuple[str, str]]:
        """Drop the classes that would be built beside a folder of their own name.

        A class is created at the root. A folder standing at the root with the same name
        IS that class and simply receives the members -- but a folder buried inside the
        tree is a different folder, and building the class anyway leaves two of that name
        in two places. Measured live: 금융 drawn at the root while 금융 stood inside
        산업별 규제 및 지원 제도, whose four children were moved out to it and whose own
        documents stayed behind.

        Dropping the class rather than refusing the design: the rest of the top level is
        usually right, and the buried folder keeping the place it has is a better answer
        than the same subject in two homes. Exact names only, so a class broader than a
        buried folder -- which is the reason this pass looks below the root at all --
        is untouched.
        """
        buried = {
            normalise_label(PurePosixPath(name).name)
            for name, _, _, _ in standing.folders
            if len(PurePosixPath(name).parts) > 1
        }
        kept: list[tuple[str, str]] = []
        for name, sign in classes:
            if normalise_label(name) in buried:
                log_trace(
                    "redesign.dropped",
                    klass=name,
                    reason="a folder of that name already stands inside the tree",
                )
                continue
            kept.append((name, sign))
        return kept

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

        for name, note, count, _ in standing.folders:
            folder = PurePosixPath(name)
            # A folder the new top level names again IS that class, standing where it
            # already stands. 행정조직 was drawn beside 행정·조직 and both survived,
            # because the two are only equal once punctuation is taken out.
            #
            # At the root, where a class is built. A buried folder of the same name is a
            # different folder, and skipping it there built the class beside it out of its
            # own children -- :meth:`_buildable` now drops such a class before this loop,
            # and the condition says which case this skip was ever about.
            if normalise_label(folder.name) in taken and len(folder.parts) == 1:
                continue
            answer = await self._llm.choose(
                prompts.build_assignment(subject=name, note=note, count=count, classes=offered),
                choices=(*by_handle, "STAY"),
            )
            if chosen := by_handle.get(answer.strip().upper()):
                placed[chosen].append(folder)

        for path, description in standing.documents:
            answer = await self._llm.choose(
                prompts.build_assignment(subject=description, note="", count=0, classes=offered),
                choices=(*by_handle, "STAY"),
            )
            if chosen := by_handle.get(answer.strip().upper()):
                placed[chosen].append(path)

        # A folder inside another folder that is also moving travels with its parent, so
        # asking for it twice would move it out from under itself.
        moving = {item for items in placed.values() for item in items}
        for name, items in placed.items():
            placed[name] = [
                item
                for item in items
                if not any(parent in moving for parent in item.parents if parent.parts)
            ]

        # Whether a class was a pass-through is a fact about what it collected: one
        # folder, no documents, and a name that says what that folder already says. Three
        # of those were built in one redesign -- 금융 및 금융소비자 over 금융업 및
        # 금융소비자, holding 89 of its 90 documents in that single child.
        folder_paths = {name for name, _, _, _ in standing.folders}
        for name, items in list(placed.items()):
            # Counted in folders, not in items. A class that collected one document whose
            # filename happens to repeat it has not built a pass-through -- 공정거래 and
            # 규제자유특구 were both dropped for holding a single PDF named after the law
            # they are about -- but a document travelling alongside the folder used to
            # skip this rule altogether, because two items is not one. 소비자 보호 over
            # 소비자 was dropped correctly at 112 documents and built at 224, the same
            # pair, because one document came with it. The reader gains a click either way.
            folders_taken = [item for item in items if str(item) in folder_paths]
            if len(folders_taken) != 1:
                continue
            only = folders_taken[0].name
            if not _says_the_same(name, only):
                continue
            log_trace(
                "redesign.dropped",
                klass=name,
                folder=only,
                reason="one folder alone",
                documents=len(items) - 1,
            )
            placed.pop(name)

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
        folder_names = {name for name, _, _, _ in standing.folders}
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
                "split_at_documents": sum(count for _, _, count, _ in standing.folders),
                "redrawn_at_documents": self._count(PurePosixPath()),
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
