"""Reading and writing the folder notes.

Two audiences, and the second is the point of the product: placement reads them to
see the structure it is filing into, and whoever searches this vault later reads
them to decide which folders to open. For that second reader a note has to be a
discriminator -- why here rather than the folder next to it -- not only a
description of what is inside (SPEC.md 3.6).
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.document import DocumentCard, sidecar_name
from bismuth.domain.errors import BismuthError, CharterError
from bismuth.domain.journal import Operation, OperationKind
from bismuth.ports.catalog import Catalog
from bismuth.ports.llm import LLM, ModelProfile
from bismuth.ports.vault import INBOX, Vault
from bismuth.prompts import charters as charter_prompts
from bismuth.services.sidecar import read_sidecar_meta

logger = logging.getLogger(__name__)

_SAMPLE_SIZE = 12

ROOT_NOTE = Charter(
    path=PurePosixPath(),
    title="전체",
    purpose="아직 나뉘지 않은 문서들. 구분이 보이면 폴더로 나뉩니다.",
)
"""The root's note, fixed rather than written.

The root is where documents wait for a distinction, so it is not about anything and
a drafted note has to invent one. Written from the first document to arrive, it said
the whole vault was about that document -- and it is written once and never
refreshed, so every later judgement was handed that as fact and spent its reasoning
arguing with it.
"""


class CharterService:
    """The folder notes: what each folder holds, so the model can reuse it."""

    def __init__(self, vault: Vault, llm: LLM, catalog: Catalog) -> None:
        self._vault = vault
        self._llm = llm
        self._catalog = catalog

    def load(self, folder: PurePosixPath) -> Charter | None:
        path = folder / CHARTER_FILENAME
        if not self._vault.exists(path):
            return None
        return Charter.from_markdown(self._vault.read_text(path), path=folder)

    def folder_views(self) -> list[tuple[str, str]]:
        """Every folder a document can be filed into, as ``(path, purpose)``.

        The root is not listed. It is always available and the prompt says so; putting
        it in the list would invite the model to treat it as one folder among many
        rather than as the place documents sit before a distinction exists.
        """
        views: list[tuple[str, str]] = []
        for folder in self._vault.iter_folders():
            if not folder.parts or folder.parts[0] == INBOX.parts[0]:
                continue
            purpose = ""
            try:
                if charter := self.load(folder):
                    purpose = charter.purpose
            except CharterError as exc:
                logger.warning("unreadable folder note at %s: %s", folder, exc)
            views.append((str(folder), purpose))
        return views

    def is_managed(self, folder: PurePosixPath) -> bool:
        """Whether Bismuth may write this folder's note."""
        try:
            charter = self.load(folder)
        except CharterError:
            return False
        return charter is None or charter.managed

    async def draft(
        self,
        folder: PurePosixPath,
        *,
        cards: list[DocumentCard],
        total_count: int | None = None,
        children: list[tuple[str, str]] | None = None,
    ) -> Charter:
        """Write a folder's note from the documents (and subfolders) in it."""
        draft = await self._llm.structured(
            charter_prompts.build(
                path=str(folder),
                document_briefs=[_brief(card) for card in cards[:_SAMPLE_SIZE]],
                total_count=total_count if total_count is not None else len(cards),
                children=children,
            ),
            schema=charter_prompts.CharterDraft,
            profile=ModelProfile.REASONING,
        )
        # A redraft rewrites what the folder holds, never how it came to hold it. The
        # axis it was divided along and the size it was divided at are history: they are
        # read back to ask whether the division still stands and to hold later classes to
        # the same question, and they cannot be recovered from the documents. Without
        # this, every document landing in a divided folder erased them on the way in --
        # notes are redrawn before subdivision runs, so the record survived only until
        # the next arrival.
        history = self._history(folder)
        return Charter(
            path=folder,
            title=draft.title.strip() or (folder.name or "Vault root"),
            purpose=draft.purpose.strip(),
            holds=tuple(draft.holds),
            answers=tuple(draft.answers),
            managed=True,
            split_basis=history.split_basis if history else "",
            split_at_documents=history.split_at_documents if history else 0,
        )

    def _history(self, folder: PurePosixPath) -> Charter | None:
        try:
            return self.load(folder)
        except BismuthError:
            return None

    def write_operation(self, charter: Charter) -> tuple[Operation, bytes]:
        """The operation and payload that persist a note."""
        return (
            Operation(
                kind=OperationKind.WRITE,
                target=charter.path / CHARTER_FILENAME,
                note=f"folder note for {charter.path or '/'}",
            ),
            charter.to_markdown().encode("utf-8"),
        )

    async def refresh_operations(
        self, folders: list[PurePosixPath]
    ) -> list[tuple[Operation, bytes]]:
        """Redraft the notes for folders whose direct contents just changed."""
        built: list[tuple[Operation, bytes]] = []
        seen: set[str] = set()
        for folder in folders:
            key = str(folder)
            if key in seen:
                continue
            seen.add(key)
            if not folder.parts or folder.parts[0] == INBOX.parts[0]:
                continue
            if not self._vault.is_dir(folder) or not self.is_managed(folder):
                continue
            cards = self._folder_cards(folder)
            children = self._child_views(folder)
            if not cards and not children:
                continue
            charter = await self.draft(
                folder, cards=cards, total_count=len(cards), children=children
            )
            built.append(self.write_operation(charter))
        return built

    def _folder_cards(self, folder: PurePosixPath) -> list[DocumentCard]:
        """The cards of the documents sitting directly in ``folder``, read via sidecars on disk."""
        cards: list[DocumentCard] = []
        for file in self._vault.iter_files(folder):
            sidecar = file.parent / sidecar_name(file.name)
            if not self._vault.exists(sidecar):
                continue
            meta = read_sidecar_meta(self._vault.read_text(sidecar))
            if not meta:
                continue
            document_id = str(meta.get("document_id", ""))
            if document_id and (card := self._catalog.load_card(document_id)):
                cards.append(card)
        return cards

    def _child_views(self, folder: PurePosixPath) -> list[tuple[str, str]]:
        """Immediate subfolders of ``folder`` as ``(name, purpose)``."""
        depth = len(folder.parts)
        children: list[tuple[str, str]] = []
        for candidate in self._vault.iter_folders():
            if len(candidate.parts) != depth + 1 or candidate.parts[:depth] != folder.parts:
                continue
            purpose = ""
            try:
                if charter := self.load(candidate):
                    purpose = charter.purpose
            except CharterError as exc:
                logger.warning("unreadable folder note at %s: %s", candidate, exc)
            children.append((candidate.name, purpose))
        return children


def _brief(card: DocumentCard) -> str:
    topics = " ".join(f"[{t}]" for t in card.topics)
    return f"- ({card.doc_type}) {card.title} {topics}\n    {card.summary}"
