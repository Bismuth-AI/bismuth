"""Moves documents between folders reversibly, when a person corrects placement."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

from bismuth.domain.document import sidecar_name
from bismuth.domain.errors import VaultError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.paths import sanitize_segment
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.charters import CharterService
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MoveResult:
    """Where documents ended up."""

    target: str
    moved: int


class MoveService:
    """Moves documents to a folder the user chose, reversibly."""

    def __init__(self, *, vault: Vault, transactor: Transactor, charters: CharterService) -> None:
        self._vault = vault
        self._transactor = transactor
        self._charters = charters

    async def move(self, paths: list[PurePosixPath], target_raw: str) -> MoveResult:
        """Move documents (and their sidecars) into ``target_raw`` as one batch.

        Raises:
            VaultError: for an unusable target, a move into the inbox, or a path
                that is not a document.
        """
        # Check the raw path for inbox intent: sanitising strips the leading
        # underscore, so "_inbox" would otherwise slip through as a folder "inbox".
        raw_first = next(
            (p.strip() for p in target_raw.replace("\\", "/").split("/") if p.strip()), ""
        )
        if raw_first == INBOX.parts[0]:
            raise VaultError("인박스로는 옮길 수 없습니다.")
        target = _safe_folder(target_raw)
        if target is None:
            raise VaultError(f"이동할 폴더 경로를 쓸 수 없습니다: {target_raw}")

        for rel in paths:
            if not self._vault.exists(rel) or self._vault.is_dir(rel):
                raise VaultError(f"옮길 파일이 없습니다: {rel}")

        operations: list[Operation] = []
        sources: set[PurePosixPath] = set()
        planned: set[str] = set()
        if not self._vault.exists(target):
            operations.append(Operation(kind=OperationKind.MKDIR, target=target))

        for rel in paths:
            if rel.parent == target:
                continue  # already there
            final = self._free_name(target, rel.name, planned)
            operations.append(
                Operation(kind=OperationKind.MOVE, source=rel, target=final, note="move")
            )
            sidecar = rel.parent / sidecar_name(rel.name)
            if self._vault.exists(sidecar):
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=sidecar,
                        target=final.parent / sidecar_name(final.name),
                        note="sidecar",
                    )
                )
            sources.add(rel.parent)

        moved = sum(1 for op in operations if op.kind is OperationKind.MOVE and op.note == "move")
        if not moved:
            return MoveResult(target=str(target), moved=0)

        self._transactor.execute(
            JournalEntry(
                actor=Actor.USER,
                reason=f"move {moved} document(s) -> {target}",
                operations=tuple(operations),
            )
        )
        # Both ends of the move changed: sources lost documents, the target (and any
        # ancestors newly created for it) gained them.
        await self._refresh(sources | {target} | set(_ancestors(target)))
        return MoveResult(target=str(target), moved=moved)

    async def rename_folder(self, folder: PurePosixPath, new_name: str) -> str:
        """Rename a folder in place -- a single journalled move of the whole subtree.

        Raises:
            VaultError: for the root/inbox, a missing folder, an unusable name, or a
                target name already taken (that would be a merge, not a rename).
        """
        if not folder.parts:
            raise VaultError("볼트 루트는 이름을 바꿀 수 없습니다.")
        if folder.parts[0] == INBOX.parts[0]:
            raise VaultError("인박스는 이름을 바꿀 수 없습니다.")
        if not self._vault.is_dir(folder):
            raise VaultError(f"그런 폴더가 없습니다: {folder}")
        try:
            safe = sanitize_segment(new_name)
        except ValueError as exc:
            raise VaultError(f"쓸 수 없는 폴더 이름: {new_name}") from exc

        target = folder.parent / safe
        if str(target) == str(folder):
            return str(folder)  # no-op
        if self._vault.exists(target):
            raise VaultError(f"이미 있는 폴더로는 바꿀 수 없습니다: {target}")

        self._transactor.execute(
            JournalEntry(
                actor=Actor.USER,
                reason=f"rename folder {folder} -> {target}",
                operations=(
                    Operation(kind=OperationKind.MOVE, source=folder, target=target, note="rename"),
                ),
            )
        )
        # The renamed folder's note now describes it under an outdated name; redraw
        # it (and any ancestors) to match.
        await self._refresh({target, *_ancestors(target)})
        return str(target)

    def _free_name(self, target: PurePosixPath, name: str, planned: set[str]) -> PurePosixPath:
        """A free path in ``target``, disambiguating against disk and names already
        planned in this same batch (two same-named files moved at once)."""
        candidate = self._vault.unique_target(target, name)
        if candidate.name.casefold() not in planned:
            planned.add(candidate.name.casefold())
            return candidate
        stem, dot, ext = name.rpartition(".")
        stem, ext = (stem, f".{ext}") if dot else (name, "")
        for index in range(2, 1000):
            bumped = f"{stem} ({index}){ext}"
            if bumped.casefold() not in planned and not self._vault.exists(target / bumped):
                planned.add(bumped.casefold())
                return target / bumped
        raise VaultError(f"{target} 안에서 빈 이름을 찾지 못했습니다: {name}")

    async def _refresh(self, folders: set[PurePosixPath]) -> None:
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


def _safe_folder(raw: str) -> PurePosixPath | None:
    """Sanitise a target folder path the same way placement does, or None."""
    segments: list[str] = []
    for part in raw.replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        try:
            segments.append(sanitize_segment(part))
        except ValueError:
            continue
    return PurePosixPath(*segments) if segments else None


def _ancestors(path: PurePosixPath) -> list[PurePosixPath]:
    result: list[PurePosixPath] = []
    parent = path.parent
    while parent.parts:
        result.append(parent)
        parent = parent.parent
    return result
