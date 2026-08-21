"""Filing a handful at a time, and looking at the whole tree when it has grown.

Two decisions, and nothing between them:

* a batch of documents is filed in one call, against the tree as it stands. Several at
  once because a class is only visible in several -- asked about one document the only
  honest answer is its title;
* when the collection crosses a size it has not been looked at since, the whole tree is
  judged once, and moved only if the answer says to.

Everything else a folder could be asked -- what property it is divided on, whether a name
answers that property, whether a level earns its place -- is left to those two calls to
decide implicitly, by naming folders a reader can tell apart.
"""

from __future__ import annotations

import collections
import logging
from collections.abc import Mapping
from pathlib import PurePosixPath

from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.document import DocumentCard, sidecar_name
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.paths import sanitize_segment
from bismuth.logging_setup import log_context, log_trace
from bismuth.ports.llm import LLM
from bismuth.ports.vault import INBOX, Vault
from bismuth.prompts import shaping
from bismuth.prompts import simple as prompts
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import read_sidecar_meta, render_sidecar
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)

BATCH = 10
"""How many documents are filed in one call.

Enough that a class can show itself -- one document only shows a title -- and few enough
that the reply stays a short list and the tree it was answered against is still the tree
when it lands.
"""

FIRST_REVIEW = 50
"""The collection is looked at whole once it holds this many, and at every doubling after.

Before that there is not enough tree to judge: whatever it says at ten documents it would
say differently at fifty, and moving things in between only costs the reader.
"""

REFILE_MINIMUM = 4
"""Fewer documents than this in a folder is not a pile, and dividing it is not worth a click."""

SHOWN = 12
"""How many of a folder's own documents the second question is shown.

Enough to see what the folder became; a folder holding eighty would otherwise bury the
handful arriving in it, which is the thing being decided."""

NEIGHBOURHOOD = 2
"""How deep the first question is allowed to see.

It asks which part of the tree a document belongs near, and the answer is a class or one
of its branches -- never a leaf. Deeper folders reach the second question anyway, which
sees the chosen place with everything inside it."""

PASSES = 4
"""How many times a level may be asked in one go.

A reply cuts one cluster and stops, so one asking is never enough for a level that has
run far past the line; a bound is still needed, since a level of folders that genuinely
have nothing in common would otherwise be asked forever."""

WIDE = 20
"""Above this many folders in one place, the list stops being a menu and becomes a page.

Only decides *when to ask* about grouping. What is a sibling of what, and what the parent
should be called, is never decided here (SPEC.md 6.1)."""

HOLDS_SHOWN = 6
"""How many children name a parent that came without a description of its own."""

REPLY_TOKENS = 2048


class SimpleFiler:
    """File in batches; look at the whole tree when it has doubled."""

    def __init__(
        self,
        vault: Vault,
        charters: CharterService,
        transactor: Transactor,
        llm: LLM,
    ) -> None:
        self._vault = vault
        self._charters = charters
        self._transactor = transactor
        self._llm = llm
        self._reviewed_at = 0

    # -- reading the tree --------------------------------------------------------------

    def _folders(self) -> list[prompts.Folder]:
        out = []
        every = list(self._vault.iter_folders())
        for folder in sorted(every, key=lambda f: str(f)):
            if not folder.parts or folder.parts[0] == INBOX.parts[0]:
                continue
            note = ""
            if (charter := self._charters.load(folder)) is not None:
                note = charter.purpose
            out.append(
                prompts.Folder(
                    path=folder,
                    note=note,
                    documents=self._count(folder),
                    held=self._count(folder, deep=True),
                    children=sum(1 for other in every if other.parent == folder),
                )
            )
        return out

    def _count(self, folder: PurePosixPath, *, deep: bool = False) -> int:
        return sum(
            1
            for path in self._vault.iter_files(folder, recursive=deep)
            if not path.name.endswith(".md")
        )

    def _total(self) -> int:
        return sum(
            1
            for path in self._vault.iter_files(PurePosixPath(), recursive=True)
            if not path.name.endswith(".md") and path.parts[0] != INBOX.parts[0]
        )

    # -- filing ------------------------------------------------------------------------

    async def file(self, batch: list[tuple[PurePosixPath, DocumentCard, object]]) -> None:
        """Put one batch of prepared documents in the tree.

        ``batch`` is ``(inbox path, card, prepared)``; the third is carried only so the
        sidecar can be written from what was read.
        """
        if not batch:
            return
        folders = self._folders()
        lines = [
            (f"D{index}", _describe(card)) for index, (_, card, _) in enumerate(batch, start=1)
        ]
        language = _language([card for _, card, _ in batch])

        # First question: which part of the tree is each of these near. Shown the upper
        # levels only -- the question is which neighbourhood, and a leaf three deep is not
        # a neighbourhood. Shown every folder there is, two thirds of the answers came
        # back "none": the reply hunts for the shelf that fits exactly, fails, and gives
        # up, when all that was wanted was the part of the tree to look in.
        near_enough = [folder for folder in folders if len(folder.path.parts) <= NEIGHBOURHOOD]
        with log_context(stage="simple.nearest"):
            reply = await self._llm.text(
                shaping.build_nearest(folders=near_enough, documents=lines, language=language),
                max_tokens=REPLY_TOKENS,
            )
        near = shaping.parse_nearest(reply)
        standing = {str(folder.path): folder for folder in folders}
        near = {
            handle: found
            for handle, found in near.items()
            if _folder_of(found) and found in standing
        }
        log_trace("simple.nearest", documents=len(batch), placed=len(near), folders=len(folders))

        # Second question: what to do at each of those places, seeing what is already in
        # them. This is the only place a parent can be built over folders that turned out
        # to be siblings.
        with log_context(stage="simple.shaping"):
            reply = await self._llm.text(
                shaping.build_shaping(
                    folders=folders,
                    places=self._places(near, lines, standing),
                    homeless=[line for line in lines if line[0] not in near],
                    language=language,
                ),
                max_tokens=REPLY_TOKENS,
            )
        shaped = shaping.parse_shaping(reply, folders)
        log_trace(
            "simple.shaped",
            # By folder, not by count: the reply names folders by handle, so a count here
            # leaves no record anywhere of where a batch actually went, and reading the
            # reply back tells you nothing either.
            inside={k: len(v) for k, v in shaped.inside.items()},
            below={k: len(v) for k, v in shaped.below.items()},
            made={k: len(v) for k, v in shaped.made.items()},
            renamed=shaped.renamed,
            loose=len(shaped.loose),
        )
        self._apply_shape(batch, shaped, near, standing)

    async def regroup(self, *, ending: bool = False) -> bool:
        """Group the folders of every level that has gone too wide to read.

        Separate from filing because it is a different question: nothing here is about a
        document, and asked alongside documents the reply answered with document numbers
        where folder numbers were wanted. Separate from the review because it is cheap and
        a level goes wide between reviews, not at them.

        Asked once, a reply groups one cluster and stops -- which leaves the level just
        over the line, to be asked again next time and cut by one cluster again. So it is
        asked until the level is no longer wide or an answer changes nothing.
        """
        changed = False
        for _turn in range(PASSES):
            wide = self._widest()
            if wide is None and ending:
                # At the end of an upload the level is asked once whatever its width. A
                # level under the line can still be a handful of real classes standing
                # beside strays that belong inside one of them, and width alone never
                # says which is which.
                wide = (PurePosixPath(), [f for f in self._folders() if len(f.path.parts) == 1])
            if wide is None or len(wide[1]) < 2 or not await self._group(*wide, settling=ending):
                return changed
            changed = True
        return changed

    def _widest(self) -> tuple[PurePosixPath, list[prompts.Folder]] | None:
        """The level holding more folders than a reader can take in, if there is one."""
        levels: dict[PurePosixPath, list[prompts.Folder]] = {}
        for folder in self._folders():
            levels.setdefault(folder.path.parent, []).append(folder)
        crowded = [(where, kids) for where, kids in levels.items() if len(kids) > WIDE]
        return max(crowded, key=lambda pair: len(pair[1])) if crowded else None

    async def _group(
        self, where: PurePosixPath, top: list[prompts.Folder], *, settling: bool = False
    ) -> bool:
        """One asking, for one level.

        ``settling`` asks a different question of the same list. Mid-run the level is too
        wide and the question is what to cut it with; at the end it may be within its
        width and still be a few real classes standing beside strays that belong inside
        one of them -- and asked to *group*, a reply weighs inventing a parent and mostly
        declines, where asked whether each stray *belongs in* a class it answers.
        """
        with log_context(stage="simple.grouping"):
            reply = await self._llm.text(
                shaping.build_grouping(folders=top, settling=settling), max_tokens=REPLY_TOKENS
            )
        groups, signs = shaping.parse_grouping(reply, top)
        standing = {str(folder.path) for folder in top}
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        moved: list[tuple[str, str]] = []
        claimed: set[str] = set()
        applied: list[tuple[str, list[str]]] = []
        for name, under in groups:
            parent = where / _folder_of(name) if str(_folder_of(name)) else where
            kept = [child for child in under if child in standing and child != str(parent)]
            # Two children are required only of a parent that has to be invented: a folder
            # built to hold one folder is a click and nothing else. Moving one stray under
            # a folder that already stands is the ordinary case, and refusing it left every
            # stray where it was.
            enough = 1 if self._vault.exists(parent) else 2
            if not str(parent) or len(kept) < enough:
                log_trace("simple.group_refused", parent=name, under=under, needed=enough)
                continue
            # A parent born without a sentence describes itself with its own name, and a
            # name repeated is not a description: the next filing decides containment by
            # reading it, and reads nothing. Its children say what it holds.
            signs.setdefault(str(parent), _holds(kept))
            for child in kept:
                source = PurePosixPath(child)
                target = parent / source.name
                if target == source or target.is_relative_to(source):
                    continue  # nothing may become its own descendant
                operations.extend(self._move_folder(source, target, signs, payloads, claimed))
                moved.append((str(source), str(target)))
                standing.discard(child)
            standing.add(str(parent))
            applied.append((str(parent), kept))
        if not operations:
            return False
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"group {sum(len(u) for _, u in applied)} folder(s) under {len(applied)}",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        log_trace("simple.grouped", where=str(where), was=len(top), groups=applied)
        return True

    def _places(
        self,
        near: dict[str, str],
        lines: list[tuple[str, str]],
        standing: dict[str, prompts.Folder],
    ) -> list[shaping.Place]:
        """One neighbourhood per folder the first question chose, with what is in it.

        The whole standing tree is listed once above these, so a parent over folders that
        turned out to be siblings can be seen: shown one folder alone, the only answers
        available are 'in' and 'below', which is how a tree grows wide and never deep.
        """
        arriving: dict[str, list[tuple[str, str]]] = {}
        for handle, line in lines:
            if (found := near.get(handle)) is not None:
                arriving.setdefault(found, []).append((handle, line))
        places = []
        for path, coming in arriving.items():
            folder = PurePosixPath(path)
            places.append(
                shaping.Place(
                    folder=folder,
                    note=standing[path].note,
                    holding=[self._described(one) for one in self._documents(folder)[:SHOWN]],
                    held=standing[path].documents,
                    children=[
                        child.name for child in self._vault.iter_folders() if child.parent == folder
                    ],
                    arriving=coming,
                )
            )
        return places

    def _apply_shape(
        self,
        batch: list[tuple[PurePosixPath, DocumentCard, object]],
        shaped: shaping.Shaped,
        near: dict[str, str],
        standing: dict[str, prompts.Folder],
    ) -> None:
        """Everything the second question asked for, in one entry.

        Folders are reshaped before documents are placed, and every destination is read
        through the reshaping: a document told to go to a folder that has just been given
        a parent belongs at its new path, not the one the answer named.
        """
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        moved: list[tuple[str, str]] = []
        claimed: set[str] = set()
        signs = dict(shaped.signs)

        for old, new in shaped.renamed:
            source = _travelled(PurePosixPath(old), moved)
            target = source.parent / _folder_of(new).name if _folder_of(new).name else source
            if str(source) not in standing or str(target) == str(source) or not target.name:
                continue
            if target.is_relative_to(source) or str(target) in standing:
                continue  # never inside itself, never onto a folder that already stands
            operations.extend(self._move_folder(source, target, signs, payloads, claimed))
            moved.append((str(source), str(target)))

        homes: dict[str, PurePosixPath] = {}
        for path, handles in shaped.inside.items():
            for handle in handles:
                homes[handle] = _travelled(PurePosixPath(path), moved)
        for group in (shaped.below, shaped.made):
            for path, handles in group.items():
                target = _travelled(PurePosixPath(path), moved)
                if not target.parts:
                    continue
                # A folder drawn for a single document filters nothing and costs a click.
                # The prompt says so; without this the reply says it and does otherwise,
                # and one-document folders were what the tree filled up with.
                if len(handles) < 2 and not self._vault.exists(target):
                    log_trace("simple.thin_folder", folder=str(target), documents=len(handles))
                    for handle in handles:
                        homes[handle] = target.parent
                    continue
                for handle in handles:
                    homes[handle] = target
        for handle in shaped.loose:
            homes[handle] = PurePosixPath()

        made: set[str] = set()
        for index, (rel, card, prepared) in enumerate(batch, start=1):
            handle = f"D{index}"
            # A document nobody said anything about goes where it was said to be near,
            # and to the root if even that was not answered. Silence is not a decision.
            target = homes.get(handle) or _travelled(PurePosixPath(near.get(handle, "")), moved)
            for level in range(1, len(target.parts) + 1):
                step = PurePosixPath(*target.parts[:level])
                if str(step) in made or self._vault.exists(step):
                    continue
                made.add(str(step))
                operations.append(Operation(kind=OperationKind.MKDIR, target=step))
                note = Charter(path=step, title=step.name, purpose=signs.get(str(step), step.name))
                operation, payload = self._charters.write_operation(note)
                operations.append(operation)
                payloads[operation.target] = payload
            operations.extend(self._move(rel, target, card, prepared, payloads))
        if not operations:
            return
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"file {len(batch)} document(s)",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )

    def _move(
        self,
        rel: PurePosixPath,
        target: PurePosixPath,
        card: DocumentCard,
        prepared: object,
        payloads: dict[PurePosixPath, bytes],
    ) -> list[Operation]:
        final = self._vault.unique_target(target, rel.name)
        operations = [Operation(kind=OperationKind.MOVE, source=rel, target=final, note="filed")]
        sidecar = final.parent / sidecar_name(final.name)
        # A document filed for the first time arrives with no sidecar; one being filed
        # again brings the old one along, and it is rewritten at the destination rather
        # than moved. Left where it was it becomes an orphan describing nothing, and the
        # next re-filing collides with it.
        stale = rel.parent / sidecar_name(rel.name)
        if stale != sidecar and self._vault.exists(stale):
            operations.append(Operation(kind=OperationKind.REMOVE, target=stale, note="refiled"))
        operations.append(Operation(kind=OperationKind.WRITE, target=sidecar, note="sidecar"))
        payloads[sidecar] = render_sidecar(
            source=prepared.source,  # type: ignore[attr-defined]
            card=card,
            extraction=prepared.extraction,  # type: ignore[attr-defined]
            document_id=prepared.source.document_id,  # type: ignore[attr-defined]
        ).encode("utf-8")
        return operations

    # -- reviewing ---------------------------------------------------------------------

    def forget_reviews(self) -> None:
        """Start the review schedule over, for a collection being filed again from scratch."""
        self._reviewed_at = 0

    def due(self, *, settling: bool = False) -> bool:
        """Whether the collection has grown enough to be worth looking at whole.

        ``settling`` is the end of an upload rather than the middle of one. Doubling alone
        never looks at what arrived after the last doubling: a collection reviewed at two
        hundred and then filled to three waits for the four hundredth document that may
        never come, and the folders that grew in between are never judged at all.
        """
        total = self._total()
        if total < FIRST_REVIEW:
            return False
        if settling:
            return total > self._reviewed_at
        return total >= max(FIRST_REVIEW, self._reviewed_at * 2)

    async def review(self, *, ending: bool = False) -> bool:
        """Look at the whole tree once. Returns whether anything moved."""
        total = self._total()
        self._reviewed_at = total
        folders = self._folders()
        if not folders:
            return False
        with log_context(stage="simple.review"):
            reply = await self._llm.text(
                prompts.build_review(
                    folders=folders,
                    total=total,
                    loose=self._count(PurePosixPath()),
                    language="",
                ),
                max_tokens=REPLY_TOKENS,
            )
        asked = prompts.parse_review(reply)
        log_trace(
            "simple.reviewed",
            total=total,
            folders=len(folders),
            keep=asked.keep,
            moves=len(asked.moves),
            refile=[str(path) for path in asked.refile],
        )
        if asked.keep and not asked.signs:
            return False
        # Moves first, refiles second: a folder's inside is drawn against the categories it
        # sits among, so those have to be standing where they will stay before it is asked.
        applied = self._apply_moves(asked, folders, total)
        changed = bool(applied)
        # Then the notes, before anything is refiled or filed against them: a note is
        # written when a folder is born and knows nothing of what arrived afterwards, and
        # the next filing decides containment by reading it.
        changed = self._resign(asked.signs, applied) or changed
        for folder in asked.refile:
            here = _travelled(folder, applied)
            if not here.parts or (self._vault.exists(here) and self._vault.is_dir(here)):
                changed = await self.refile(here) or changed
        # Refiling the root turns a pile into folders at the top, which is the one thing
        # that reliably makes the top too wide to read. Group after, not before.
        return await self.regroup(ending=ending) or changed

    def _resign(self, signs: Mapping[str, str], applied: list[tuple[str, str]]) -> bool:
        """Rewrite the notes of folders that already stand, in one entry."""
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        rewritten: list[str] = []
        for named, sentence in signs.items():
            folder = _travelled(PurePosixPath(named), applied)
            if not folder.parts or not self._vault.exists(folder):
                continue  # a note for a folder that does not exist yet belongs to its move
            standing = self._charters.load(folder)
            if standing is not None and standing.purpose == sentence:
                continue
            note = Charter(path=folder, title=folder.name, purpose=sentence)
            operation, payload = self._charters.write_operation(note)
            operations.append(operation)
            payloads[operation.target] = payload
            rewritten.append(str(folder))
        if not operations:
            return False
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"re-describe {len(rewritten)} folder(s)",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        log_trace("simple.resigned", folders=rewritten)
        return True

    def _apply_moves(
        self, asked: prompts.Reviewed, folders: list[prompts.Folder], total: int
    ) -> list[tuple[str, str]]:
        """Move whole folders in one entry. Returns the moves that actually ran."""
        if not asked.moves:
            return []
        signs = dict(asked.signs)
        standing = {str(folder.path) for folder in folders}
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        applied: list[tuple[str, str]] = []
        moved: set[str] = set()
        claimed: set[str] = set()
        """Destinations whose folder note is already spoken for by an earlier move."""
        wanted = collections.Counter(target for _, target in asked.moves)
        for source, named in asked.moves:
            # ``MOVE: A | B`` means A *becomes* B. Asked to gather folders under a parent,
            # the reply names the parent alone -- and two folders both becoming B is not a
            # rename anybody could mean, it is two folders going under B. Read that way,
            # and the same for a B that already stands: renaming one folder onto another
            # merges two shelves into a pile, which is never what "move it there" meant.
            target = named
            if wanted[named] > 1 or (
                named not in (source, "") and self._vault.exists(PurePosixPath(named))
            ):
                target = str(PurePosixPath(named) / PurePosixPath(source).name)
            if source not in standing or not target or source == target:
                continue
            # A folder inside one that is already moving travels with its parent.
            if any(source.startswith(f"{other}/") for other in moved):
                continue
            if target == source or target.startswith(f"{source}/"):
                continue  # a folder cannot be moved inside itself
            moved.add(source)
            applied.append((source, target))
            operations.extend(
                self._move_folder(
                    PurePosixPath(source), PurePosixPath(target), signs, payloads, claimed
                )
            )
        if not operations:
            log_trace("simple.review_refused", reason="no move survived the shape checks")
            return []
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"review the tree at {total} documents: {len(applied)} move(s)",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        log_trace("simple.review_applied", moves=applied)
        return applied

    def _move_folder(
        self,
        source: PurePosixPath,
        target: PurePosixPath,
        signs: dict[str, str],
        payloads: dict[PurePosixPath, bytes],
        claimed: set[str],
    ) -> list[Operation]:
        """Move a folder whole: every document keeps the folder it is in."""
        operations: list[Operation] = []
        for level in range(1, len(target.parts)):
            above = PurePosixPath(*target.parts[:level])
            if not self._vault.exists(above):
                operations.append(Operation(kind=OperationKind.MKDIR, target=above))
                note = Charter(
                    path=above, title=above.name, purpose=signs.get(str(above), above.name)
                )
                operation, payload = self._charters.write_operation(note)
                operations.append(operation)
                payloads[operation.target] = payload
        subtree = sorted(
            (f for f in self._vault.iter_folders() if f == source or f.is_relative_to(source)),
            key=lambda f: len(f.parts),
        )
        emptied: list[PurePosixPath] = []
        for sub in subtree:
            destination = target / sub.relative_to(source)
            operations.append(Operation(kind=OperationKind.MKDIR, target=destination))
            for path in sorted(self._vault.iter_files(sub, recursive=False)):
                landing = self._vault.unique_target(destination, path.name)
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=path,
                        target=landing,
                        note="review",
                    )
                )
                # A document's sidecar is hidden from iter_files, so it has to be named
                # alongside the document. Left behind it also keeps the old folder from
                # ever being empty, and a folder that is not empty is never removed.
                beside = sub / sidecar_name(path.name)
                if self._vault.exists(beside):
                    operations.append(
                        Operation(
                            kind=OperationKind.MOVE,
                            source=beside,
                            target=landing.parent / sidecar_name(landing.name),
                            note="sidecar",
                        )
                    )
            # The folder note is not one of the folder's files, so it has to be named. Left
            # behind it also keeps the old folder from being empty, and an un-empty folder
            # is never removed -- the move would leave a husk with a note in it.
            #
            # Several folders may be told to move into the same place, which is a merge:
            # the destination can only carry one note, so the first one to arrive keeps it
            # and the rest are dropped. Without this the entry plans two writes of the same
            # path, the transactor refuses the second, and the whole review rolls back.
            standing_note = sub / CHARTER_FILENAME
            if self._vault.exists(standing_note):
                taken = str(destination) in claimed or self._vault.exists(
                    destination / CHARTER_FILENAME
                )
                claimed.add(str(destination))
                operations.append(
                    Operation(kind=OperationKind.REMOVE, target=standing_note, note="merged away")
                    if taken
                    else Operation(
                        kind=OperationKind.MOVE,
                        source=standing_note,
                        target=destination / CHARTER_FILENAME,
                        note="folder note",
                    )
                )
            emptied.append(sub)
        operations.extend(
            Operation(kind=OperationKind.RMDIR, target=path) for path in reversed(emptied)
        )
        return operations

    # -- refiling one folder -------------------------------------------------------------

    async def refile(self, folder: PurePosixPath) -> bool:
        """Draw sub-folders inside one folder for the documents piled in it.

        Local reclassification (docs/reclassification.md §8): the answer's range is this
        folder. Nothing leaves it, so a folder that has outgrown its own name is fixed
        without the rest of the tree being reopened.

        The whole folder is one journal entry (§4): a refile that half ran leaves the pile
        split between the old arrangement and the new one, and the reader then finds the
        collection in neither.
        """
        loose = self._documents(folder)
        if len(loose) < REFILE_MINIMUM:
            log_trace("simple.refile_skipped", folder=str(folder), documents=len(loose))
            return False
        standing = [
            prompts.Folder(path=child, note=self._note(child), documents=self._count(child))
            for child in sorted(self._vault.iter_folders())
            # The root is its own parent and the inbox is not part of the tree, so both
            # have to be said out loud once the folder being redrawn can be the root.
            if child.parts and child.parent == folder and child.parts[0] != INBOX.parts[0]
        ]
        drawn: dict[PurePosixPath, int] = {}
        signs: dict[str, str] = {}
        settled: dict[PurePosixPath, PurePosixPath] = {}
        renamed = ""
        language = self._language_of(loose)
        with log_context(stage="simple.refile"):
            for start in range(0, len(loose), BATCH):
                chunk = loose[start : start + BATCH]
                reply = await self._llm.text(
                    prompts.build_refiling(
                        folder=folder,
                        # Carried forward so a later batch can join a sub-folder an
                        # earlier one drew, instead of naming it again slightly differently.
                        children=standing
                        + [
                            prompts.Folder(path=path, note=signs.get(str(path), ""), documents=held)
                            for path, held in drawn.items()
                        ],
                        documents=[
                            (f"D{index}", self._described(path))
                            for index, path in enumerate(chunk, start=1)
                        ],
                        remaining=len(loose) - start,
                        language=language,
                    ),
                    max_tokens=REPLY_TOKENS,
                )
                answers, said = prompts.parse_filing(reply)
                renamed = renamed or _folder_of(said.pop(prompts.RENAME, "")).name
                for name, sentence in said.items():
                    if (path := _below(folder, name)) is not None:
                        signs.setdefault(str(path), sentence)
                for index, document in enumerate(chunk, start=1):
                    target = _below(folder, answers.get(f"D{index}", ""))
                    if target is None:
                        continue
                    settled[document] = target
                    drawn[target] = drawn.get(target, 0) + 1
        return self._settle(folder, loose, settled, signs, renamed)

    def _settle(
        self,
        folder: PurePosixPath,
        loose: list[PurePosixPath],
        settled: dict[PurePosixPath, PurePosixPath],
        signs: dict[str, str],
        renamed: str = "",
    ) -> bool:
        """Everything the refile decided, applied at once or not at all."""
        # A single sub-folder holding the whole pile is this folder's name written twice:
        # the reader clicks once more and meets the same list.
        if len(set(settled.values())) < 2 and len(settled) == len(loose):
            log_trace("simple.refile_refused", folder=str(folder), reason="one sub-folder took all")
            return False
        # A folder drawn for one document filters nothing and costs a click.
        alone = {target for target in settled.values() if list(settled.values()).count(target) < 2}
        settled = {
            document: target
            for document, target in settled.items()
            if target not in alone or self._vault.exists(target)
        }
        if not settled:
            log_trace("simple.refile_refused", folder=str(folder), reason="nothing to move")
            return False

        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        made: set[str] = set()
        # The shelf may be renamed by the same answer that divides it, and that has to
        # happen first: everything below is addressed relative to where it now stands.
        # One entry, so a shelf can never end up divided under a name it no longer has.
        if renamed and folder.parts and renamed != folder.name:
            moved_to = folder.parent / renamed
            if not self._vault.exists(moved_to):
                operations.extend(self._move_folder(folder, moved_to, signs, payloads, set()))
                settled = {
                    moved_to / document.relative_to(folder): moved_to / target.relative_to(folder)
                    for document, target in settled.items()
                }
                signs = {
                    str(moved_to / PurePosixPath(path).relative_to(folder))
                    if PurePosixPath(path).is_relative_to(folder)
                    else path: sentence
                    for path, sentence in signs.items()
                }
                log_trace("simple.refile_renamed", was=str(folder), now=str(moved_to))
                folder = moved_to
        for target in sorted(set(settled.values()), key=lambda path: len(path.parts)):
            for level in range(len(folder.parts) + 1, len(target.parts) + 1):
                step = PurePosixPath(*target.parts[:level])
                if str(step) in made or self._vault.exists(step):
                    continue
                made.add(str(step))
                operations.append(Operation(kind=OperationKind.MKDIR, target=step))
                note = Charter(path=step, title=step.name, purpose=signs.get(str(step), step.name))
                operation, payload = self._charters.write_operation(note)
                operations.append(operation)
                payloads[operation.target] = payload
        for document, target in sorted(settled.items()):
            final = self._vault.unique_target(target, document.name)
            operations.append(
                Operation(kind=OperationKind.MOVE, source=document, target=final, note="refile")
            )
            beside = document.parent / sidecar_name(document.name)
            if self._vault.exists(beside):
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=beside,
                        target=final.parent / sidecar_name(final.name),
                        note="sidecar",
                    )
                )
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=(
                    f"refile {folder}: {len(settled)} document(s) "
                    f"into {len(set(settled.values()))} sub-folder(s)"
                ),
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        log_trace(
            "simple.refiled",
            folder=str(folder),
            moved=len(settled),
            stayed=len(loose) - len(settled),
            sub_folders=sorted(str(path) for path in set(settled.values())),
        )
        return True

    # -- reading documents ---------------------------------------------------------------

    def _documents(self, folder: PurePosixPath) -> list[PurePosixPath]:
        return sorted(
            path
            for path in self._vault.iter_files(folder, recursive=False)
            if not path.name.endswith(".md")
        )

    def _note(self, folder: PurePosixPath) -> str:
        charter = self._charters.load(folder)
        return charter.purpose if charter is not None else ""

    def _meta(self, path: PurePosixPath) -> dict[str, object]:
        """What the document's sidecar says about it, which is the card as it was filed."""
        sidecar = path.parent / sidecar_name(path.name)
        if not self._vault.exists(sidecar):
            return {}
        return read_sidecar_meta(self._vault.read_text(sidecar)) or {}

    def _described(self, path: PurePosixPath) -> str:
        meta = self._meta(path)
        topics = meta.get("topics")
        parts = [
            str(meta.get("title") or ""),
            str(meta.get("doc_type") or ""),
            ", ".join(
                str(topic) for topic in (topics if isinstance(topics, list) else [])[:TOPICS_SHOWN]
            ),
        ]
        return " | ".join(part for part in parts if part) or path.name

    def _language_of(self, paths: list[PurePosixPath]) -> str:
        spoken = [str(self._meta(path).get("language") or "") for path in paths]
        return _common([word for word in spoken if word and word != "unknown"])


TOPICS_SHOWN = 3
"""How many of a card's topics reach the filing prompt.

The card is already a summary; a filing question does not need the summary inside it. Sent
whole -- every topic and the summary paragraph -- ten cards came to 5,700 characters
against 250 characters of folders to choose from, and the reply put all ten at the root
including two copies of the same law. What is being asked is which folder, and the title,
the kind and the first few topics are what answers it.
"""


def _describe(card: DocumentCard) -> str:
    """One line per document: what it is called, what kind it is, what it is about.

    Not the summary: that is prose about what the document does inside itself, which is
    the one thing the choice does not turn on. Not the entities either -- on this corpus
    they were mostly the other laws a law cites, which pull toward the wrong folder.
    """
    parts = [card.title, card.doc_type, ", ".join(card.topics[:TOPICS_SHOWN])]
    return " | ".join(part for part in parts if part)


def _holds(children: list[str]) -> str:
    """A folder described by what stands inside it, for one that arrived without a sentence."""
    names = [PurePosixPath(child).name for child in children]
    shown = ", ".join(names[:HOLDS_SHOWN])
    return shown + (f" 등 {len(names)}개 갈래" if len(names) > HOLDS_SHOWN else "")


def _language(cards: list[DocumentCard]) -> str:
    """The language to answer in, when the batch agrees on one."""
    return _common(
        [card.language for card in cards if card.language and card.language != "unknown"]
    )


def _common(spoken: list[str]) -> str:
    """The one language nearly all of them are in, or nothing."""
    if not spoken:
        return ""
    common = max(set(spoken), key=spoken.count)
    return common if spoken.count(common) / len(spoken) >= 0.75 else ""


def _folder_of(answer: str) -> PurePosixPath:
    """The folder a reply named, or the root.

    ROOT, an empty answer and a path that sanitises away all mean the same thing: this
    document has no home yet, and a pile at the root is the honest place for it.
    """
    cleaned = answer.strip().strip("/").strip()
    if not cleaned or cleaned.upper() in {"ROOT", "(ROOT)", "."}:
        return PurePosixPath()
    return PurePosixPath(*_segments(cleaned))


def _below(folder: PurePosixPath, answer: str) -> PurePosixPath | None:
    """The sub-folder of ``folder`` a refile named, or ``None`` for stay where you are.

    STAY, an empty answer and a path that sanitises away all mean the same thing. So does
    an answer that opens with this folder's own name: below ``금융``, ``금융/은행`` and
    ``은행`` are one place, and a document cannot be told to move into where it already is.
    """
    cleaned = answer.strip().strip("/").strip()
    if not cleaned or cleaned.upper() in {"STAY", "(STAY)", ".", "ROOT"}:
        return None
    segments = _segments(cleaned)
    while segments and segments[0] == folder.name:
        segments = segments[1:]
    return folder.joinpath(*segments) if segments else None


def _segments(path: str) -> list[str]:
    parts = [sanitize_segment(part) for part in path.split("/") if part.strip()]
    return [part for part in parts if part and part != CHARTER_FILENAME]


def _travelled(folder: PurePosixPath, moves: list[tuple[str, str]]) -> PurePosixPath:
    """Where a folder is after the moves that ran, since a refile is asked of it afterwards."""
    for source, target in moves:
        if folder == PurePosixPath(source):
            return PurePosixPath(target)
        if folder.is_relative_to(source):
            return PurePosixPath(target) / folder.relative_to(source)
    return folder
