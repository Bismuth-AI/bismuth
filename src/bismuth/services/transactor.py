"""The only component allowed to mutate a vault: stash, journal, execute, roll back on failure, mark applied."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from bismuth.domain.errors import VaultError
from bismuth.domain.journal import (
    Actor,
    EntryStatus,
    JournalEntry,
    Operation,
    OperationKind,
)
from bismuth.ports.journal import JournalStore
from bismuth.ports.vault import Vault

logger = logging.getLogger(__name__)

Payloads = dict[PurePosixPath, bytes]

_DESTRUCTIVE = (OperationKind.WRITE, OperationKind.REMOVE)


class Transactor:
    """Runs journalled batches against a vault."""

    def __init__(self, vault: Vault, journal: JournalStore) -> None:
        self._vault = vault
        self._journal = journal

    def execute(self, entry: JournalEntry, payloads: Payloads | None = None) -> JournalEntry:
        """Run a batch, or leave the vault as it was.

        Args:
            entry: the batch. Its ``backup_ref`` fields are filled in here; callers
                should not try to populate them.
            payloads: content for WRITE operations, keyed by target path.

        Returns:
            The entry, marked APPLIED.

        Raises:
            VaultError: if any operation failed. The batch has been rolled back;
                the entry is marked FAILED and carries the reason.
        """
        payloads = payloads or {}
        prepared = self._stash_targets(entry)

        self._journal.append(prepared)  # durable before anything moves

        completed: list[Operation] = []
        try:
            for operation in prepared.operations:
                self._vault.apply(operation, payload=payloads.get(operation.target))
                completed.append(operation)
        except Exception as exc:
            logger.warning(
                "batch %s failed at operation %d: %s", prepared.id, len(completed) + 1, exc
            )
            self._roll_back(completed)
            failed = prepared.with_status(EntryStatus.FAILED, error=str(exc))
            self._journal.update(failed)
            raise VaultError(f"{prepared.reason}: {exc}") from exc

        applied = prepared.with_status(EntryStatus.APPLIED)
        self._journal.update(applied)
        return applied

    def undo(self, entry_id: str) -> JournalEntry:
        """Reverse an applied batch, as its own journalled entry.

        Raises:
            VaultError: if the entry is unknown or was never applied.
        """
        entry = self._journal.get(entry_id)
        if entry is None:
            raise VaultError(f"no journal entry {entry_id}")
        if entry.status is not EntryStatus.APPLIED:
            raise VaultError(
                f"entry {entry_id} is {entry.status.value}, not applied -- nothing to undo"
            )
        # An entry is only reversible while nothing has moved what it moved. Subdivision
        # keeps re-filing documents as more arrive, so an older entry's sources are
        # usually somewhere else by now: the inverse then fails partway with "cannot move
        # missing file", rolls back, and says nothing about why. Say it here instead.
        if missing := self._moved_since(entry):
            raise VaultError(
                f"entry {entry_id} cannot be undone: {len(missing)} of the files it moved "
                f"have since been moved again, starting with {missing[0]}. Undo the later "
                "entries first."
            )

        inverse = JournalEntry(
            actor=Actor.USER,
            reason=f"undo: {entry.reason}",
            operations=entry.inverse_operations(),
            document_id=entry.document_id,
        )
        applied = self.execute(inverse)
        self._journal.update(entry.with_status(EntryStatus.REVERTED))
        return applied

    def _moved_since(self, entry: JournalEntry) -> list[str]:
        """The destinations this entry wrote that are no longer where it left them."""
        gone: list[str] = []
        for operation in entry.operations:
            if operation.kind is not OperationKind.MOVE:
                continue
            if not self._vault.exists(operation.target):
                gone.append(str(operation.target))
        return gone

    def recover(self) -> list[JournalEntry]:
        """Undo anything a crash left half-done. Safe to call on every startup."""
        recovered: list[JournalEntry] = []
        for entry in self._journal.pending():
            logger.warning(
                "recovering interrupted batch %s (%s): rolling back %d operations",
                entry.id,
                entry.reason,
                len(entry.operations),
            )
            self._roll_back(list(entry.operations))
            reverted = entry.with_status(
                EntryStatus.REVERTED, error="interrupted; rolled back on restart"
            )
            self._journal.update(reverted)
            recovered.append(reverted)
        return recovered

    def _stash_targets(self, entry: JournalEntry) -> JournalEntry:
        """Copy soon-to-be-clobbered content into the attic, recording the keys."""
        operations = tuple(
            operation.model_copy(update={"backup_ref": self._vault.stash(operation.target)})
            if operation.kind in _DESTRUCTIVE and operation.backup_ref is None
            else operation
            for operation in entry.operations
        )
        return entry.model_copy(update={"operations": operations})

    def _roll_back(self, completed: list[Operation]) -> None:
        for operation in reversed(completed):
            inverse = operation.inverse()
            if inverse is None:
                continue
            try:
                self._vault.apply(inverse)
            except Exception as exc:
                # Keep going: stopping here would strand the vault mid-rollback.
                logger.error(
                    "could not reverse %s on %s during rollback: %s",
                    operation.kind.value,
                    operation.target,
                    exc,
                )
