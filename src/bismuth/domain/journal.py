"""The journal: every change Bismuth makes, written down before it happens."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field


class OperationKind(StrEnum):
    """The complete set of mutations Bismuth can perform."""

    MKDIR = "mkdir"
    MOVE = "move"
    WRITE = "write"
    REMOVE = "remove"
    RMDIR = "rmdir"
    RESTORE = "restore"
    """Only ever produced by inversion: put stashed content back."""


class Operation(BaseModel):
    """One mutation, expressed against vault-relative paths."""

    model_config = ConfigDict(frozen=True)

    kind: OperationKind
    target: PurePosixPath
    source: PurePosixPath | None = Field(default=None, description="Origin for MOVE/RESTORE.")
    backup_ref: str | None = Field(
        default=None,
        description=(
            "Attic key holding the content that used to be at ``target``. None "
            "means the target did not exist, which inverts to a plain REMOVE."
        ),
    )
    note: str = ""

    def inverse(self) -> Operation | None:
        """The operation that undoes this one, or ``None`` if nothing need be done."""
        match self.kind:
            case OperationKind.MOVE:
                if self.source is None:
                    return None
                return Operation(
                    kind=OperationKind.MOVE,
                    source=self.target,
                    target=self.source,
                    note=f"undo move of {self.target.name}",
                )
            case OperationKind.MKDIR:
                # Best-effort: executor skips a non-empty directory rather than deleting user files.
                return Operation(
                    kind=OperationKind.RMDIR, target=self.target, note="undo mkdir (if empty)"
                )
            case OperationKind.WRITE | OperationKind.RESTORE:
                if self.backup_ref is None:
                    return Operation(
                        kind=OperationKind.REMOVE, target=self.target, note="undo write"
                    )
                return Operation(
                    kind=OperationKind.RESTORE,
                    target=self.target,
                    backup_ref=self.backup_ref,
                    note="restore previous content",
                )
            case OperationKind.REMOVE:
                if self.backup_ref is None:
                    return None
                return Operation(
                    kind=OperationKind.RESTORE, target=self.target, backup_ref=self.backup_ref
                )
            case OperationKind.RMDIR:
                return Operation(kind=OperationKind.MKDIR, target=self.target)
        return None


class EntryStatus(StrEnum):
    PENDING = "pending"
    """Written, not finished. On startup these mean we crashed; they are the
    recovery worklist."""
    APPLIED = "applied"
    REVERTED = "reverted"
    FAILED = "failed"


class Actor(StrEnum):
    """Who caused the change, recorded on every entry for provenance."""

    BISMUTH = "bismuth"
    USER = "user"
    RECOVERY = "recovery"


class JournalEntry(BaseModel):
    """One atomic-in-intent batch of operations."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: Actor = Actor.BISMUTH
    reason: str = Field(description="Human-readable. Shown verbatim in `bismuth log`.")
    operations: tuple[Operation, ...]
    status: EntryStatus = EntryStatus.PENDING
    document_id: str | None = None
    error: str | None = None

    def inverse_operations(self) -> tuple[Operation, ...]:
        """Operations that undo this entry, in reverse order."""
        inverted = (op.inverse() for op in reversed(self.operations))
        return tuple(op for op in inverted if op is not None)

    def with_status(self, status: EntryStatus, *, error: str | None = None) -> JournalEntry:
        return self.model_copy(update={"status": status, "error": error})

    def touches(self, path: PurePosixPath) -> bool:
        return any(op.target == path or op.source == path for op in self.operations)


def batch(
    reason: str,
    operations: Iterable[Operation],
    *,
    actor: Actor = Actor.BISMUTH,
    document_id: str | None = None,
) -> JournalEntry:
    """Convenience constructor. Services build entries; only the executor runs them."""
    return JournalEntry(
        reason=reason, operations=tuple(operations), actor=actor, document_id=document_id
    )
