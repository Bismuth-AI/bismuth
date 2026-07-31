"""Deletes a file or folder reversibly, through the journal."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

from bismuth.domain.charter import CHARTER_FILENAME
from bismuth.domain.document import sidecar_name
from bismuth.domain.errors import VaultError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.ports.catalog import Catalog
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import read_sidecar_meta
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeletionResult:
    """What a delete removed."""

    path: str
    files: int
    """Documents removed (sidecars and notes are not counted)."""


class DeletionService:
    """Removes files and folders, reversibly."""

    def __init__(
        self,
        *,
        vault: Vault,
        catalog: Catalog,
        transactor: Transactor,
        charters: CharterService,
    ) -> None:
        self._vault = vault
        self._catalog = catalog
        self._transactor = transactor
        self._charters = charters

    async def delete_file(self, rel: PurePosixPath) -> DeletionResult:
        """Delete one document: its file, its sidecar, and its card.

        Raises:
            VaultError: if the path is not a file in the vault.
        """
        if not self._vault.exists(rel) or self._vault.is_dir(rel):
            raise VaultError(f"삭제할 파일이 없습니다: {rel}")

        operations = list(self._file_operations(rel))
        self._transactor.execute(
            JournalEntry(
                actor=Actor.USER,
                reason=f"delete {rel.name}",
                operations=tuple(operations),
            )
        )
        await self._refresh([rel.parent])
        return DeletionResult(path=str(rel), files=1)

    async def delete_files(self, paths: list[PurePosixPath]) -> DeletionResult:
        """Delete several documents as one reversible batch.

        Raises:
            VaultError: if any path is not a file in the vault (nothing is deleted).
        """
        for rel in paths:
            if not self._vault.exists(rel) or self._vault.is_dir(rel):
                raise VaultError(f"삭제할 파일이 없습니다: {rel}")
        if not paths:
            return DeletionResult(path="", files=0)

        operations: list[Operation] = []
        for rel in paths:
            operations.extend(self._file_operations(rel))
        self._transactor.execute(
            JournalEntry(
                actor=Actor.USER,
                reason=f"delete {len(paths)} document(s)",
                operations=tuple(operations),
            )
        )
        await self._refresh({rel.parent for rel in paths})
        return DeletionResult(path=str(paths[0].parent), files=len(paths))

    async def delete_folder(self, rel: PurePosixPath) -> DeletionResult:
        """Delete a folder and everything under it, as one reversible batch.

        Raises:
            VaultError: for the root, the inbox, or a path that is not a folder.
        """
        if not rel.parts:
            raise VaultError("볼트 루트는 삭제할 수 없습니다.")
        if rel.parts[0] == INBOX.parts[0]:
            raise VaultError("인박스는 삭제할 수 없습니다.")
        if not self._vault.is_dir(rel):
            raise VaultError(f"삭제할 폴더가 없습니다: {rel}")

        operations: list[Operation] = []
        documents = 0

        for file in self._vault.iter_files(rel, recursive=True):
            operations.extend(self._file_operations(file))
            documents += 1

        # Deepest folders first: RMDIR no-ops on a non-empty directory.
        folders = sorted(
            (f for f in self._vault.iter_folders() if f == rel or _is_under(f, rel)),
            key=lambda f: len(f.parts),
            reverse=True,
        )
        for folder in folders:
            note = folder / CHARTER_FILENAME
            if self._vault.exists(note):
                operations.append(
                    Operation(kind=OperationKind.REMOVE, target=note, note="folder note")
                )
            operations.append(Operation(kind=OperationKind.RMDIR, target=folder))

        self._transactor.execute(
            JournalEntry(
                actor=Actor.USER,
                reason=f"delete folder {rel}/ ({documents} document(s))",
                operations=tuple(operations),
            )
        )
        await self._refresh([rel.parent])
        return DeletionResult(path=str(rel), files=documents)

    async def _refresh(self, folders: set[PurePosixPath] | list[PurePosixPath]) -> None:
        """Redraw the notes of folders that just lost documents or a child."""
        operations = await self._charters.refresh_operations(list(folders))
        if not operations:
            return
        self._transactor.execute(
            JournalEntry(
                reason="refresh folder notes",
                operations=tuple(op for op, _ in operations),
            ),
            payloads={op.target: payload for op, payload in operations},
        )

    def _file_operations(self, rel: PurePosixPath) -> list[Operation]:
        """Remove a document, its sidecar, and forget its card (card dropped outside the journal; it's a rebuildable cache)."""
        operations = [Operation(kind=OperationKind.REMOVE, target=rel, note="document")]

        sidecar = rel.parent / sidecar_name(rel.name)
        if self._vault.exists(sidecar):
            meta = read_sidecar_meta(self._vault.read_text(sidecar))
            if meta and (document_id := str(meta.get("document_id", ""))):
                self._catalog.forget(document_id)
            operations.append(Operation(kind=OperationKind.REMOVE, target=sidecar, note="sidecar"))
        return operations


def _is_under(path: PurePosixPath, ancestor: PurePosixPath) -> bool:
    return path.parts[: len(ancestor.parts)] == ancestor.parts and len(path.parts) > len(
        ancestor.parts
    )
