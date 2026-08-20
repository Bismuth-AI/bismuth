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

import logging
from pathlib import PurePosixPath

from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.document import DocumentCard, sidecar_name
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.paths import sanitize_segment
from bismuth.logging_setup import log_context, log_trace
from bismuth.ports.llm import LLM
from bismuth.ports.vault import INBOX, Vault
from bismuth.prompts import simple as prompts
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import render_sidecar
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
        for folder in sorted(self._vault.iter_folders(), key=lambda f: str(f)):
            if not folder.parts or folder.parts[0] == INBOX.parts[0]:
                continue
            note = ""
            if (charter := self._charters.load(folder)) is not None:
                note = charter.purpose
            out.append(prompts.Folder(path=folder, note=note, documents=self._count(folder)))
        return out

    def _count(self, folder: PurePosixPath) -> int:
        return sum(
            1
            for path in self._vault.iter_files(folder, recursive=False)
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
        with log_context(stage="simple.filing"):
            reply = await self._llm.text(
                prompts.build_filing(
                    folders=folders,
                    documents=lines,
                    loose=self._count(PurePosixPath()),
                    language=_language([card for _, card, _ in batch]),
                ),
                max_tokens=REPLY_TOKENS,
            )
        placed, signs = prompts.parse_filing(reply)
        log_trace(
            "simple.filed",
            documents=len(batch),
            answered=len(placed),
            new_signs=len(signs),
            folders=len(folders),
        )

        standing = {str(folder.path) for folder in folders}
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        created: set[str] = set()
        for index, (rel, card, prepared) in enumerate(batch, start=1):
            target = _folder_of(placed.get(f"D{index}", ""))
            if str(target) and str(target) not in standing and str(target) not in created:
                for level in range(1, len(target.parts) + 1):
                    made = PurePosixPath(*target.parts[:level])
                    if str(made) in standing or str(made) in created:
                        continue
                    created.add(str(made))
                    operations.append(Operation(kind=OperationKind.MKDIR, target=made))
                    note = Charter(
                        path=made,
                        title=made.name,
                        purpose=signs.get(str(made), made.name),
                    )
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
        operations.append(Operation(kind=OperationKind.WRITE, target=sidecar, note="sidecar"))
        payloads[sidecar] = render_sidecar(
            source=prepared.source,  # type: ignore[attr-defined]
            card=card,
            extraction=prepared.extraction,  # type: ignore[attr-defined]
            document_id=prepared.source.document_id,  # type: ignore[attr-defined]
        ).encode("utf-8")
        return operations

    # -- reviewing ---------------------------------------------------------------------

    def due(self) -> bool:
        """Whether the collection has grown enough to be worth looking at whole."""
        total = self._total()
        if total < FIRST_REVIEW:
            return False
        return total >= max(FIRST_REVIEW, self._reviewed_at * 2)

    async def review(self) -> bool:
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
        keep, moves, signs = prompts.parse_review(reply)
        log_trace("simple.reviewed", total=total, folders=len(folders), keep=keep, moves=len(moves))
        if keep or not moves:
            return False

        standing = {str(folder.path) for folder in folders}
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        applied: list[tuple[str, str]] = []
        moved: set[str] = set()
        for source, target in moves:
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
                self._move_folder(PurePosixPath(source), PurePosixPath(target), signs, payloads)
            )
        if not operations:
            log_trace("simple.review_refused", reason="no move survived the shape checks")
            return False
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"review the tree at {total} documents: {len(applied)} move(s)",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        log_trace("simple.review_applied", moves=applied)
        return True

    def _move_folder(
        self,
        source: PurePosixPath,
        target: PurePosixPath,
        signs: dict[str, str],
        payloads: dict[PurePosixPath, bytes],
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
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=path,
                        target=destination / path.name,
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
                            target=destination / sidecar_name(path.name),
                            note="sidecar",
                        )
                    )
            # The folder note is not one of the folder's files, so it has to be named. Left
            # behind it also keeps the old folder from being empty, and an un-empty folder
            # is never removed -- the move would leave a husk with a note in it.
            standing_note = sub / CHARTER_FILENAME
            if self._vault.exists(standing_note):
                operations.append(
                    Operation(
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


def _language(cards: list[DocumentCard]) -> str:
    """The language to answer in, when the batch agrees on one."""
    seen = [card.language for card in cards if card.language and card.language != "unknown"]
    if not seen:
        return ""
    common = max(set(seen), key=seen.count)
    return common if seen.count(common) / len(seen) >= 0.75 else ""


def _folder_of(answer: str) -> PurePosixPath:
    """The folder a reply named, or the root.

    ROOT, an empty answer and a path that sanitises away all mean the same thing: this
    document has no home yet, and a pile at the root is the honest place for it.
    """
    cleaned = answer.strip().strip("/").strip()
    if not cleaned or cleaned.upper() in {"ROOT", "(ROOT)", "."}:
        return PurePosixPath()
    segments = [sanitize_segment(part) for part in cleaned.split("/") if part.strip()]
    return PurePosixPath(
        *[segment for segment in segments if segment and segment != CHARTER_FILENAME]
    )
