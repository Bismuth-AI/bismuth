"""Filesystem boundary using vault-relative POSIX paths."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from bismuth.domain.journal import Operation

INBOX = PurePosixPath("_inbox")

STATE_DIR = ".bismuth"


@runtime_checkable
class Vault(Protocol):
    """A directory Bismuth organises."""

    @property
    def root(self) -> Path:
        """Return the absolute vault root."""
        ...

    def exists(self, rel: PurePosixPath) -> bool: ...

    def is_dir(self, rel: PurePosixPath) -> bool: ...

    def read_text(self, rel: PurePosixPath) -> str: ...

    def read_bytes(self, rel: PurePosixPath) -> bytes: ...

    def iter_folders(self) -> Iterator[PurePosixPath]:
        """Yield folders depth-first, excluding internal state."""
        ...

    def iter_files(self, rel: PurePosixPath, *, recursive: bool = False) -> Iterator[PurePosixPath]:
        """Files under ``rel``, excluding Bismuth's own artefacts (sidecars, charters)."""
        ...

    def count_files(self, rel: PurePosixPath, *, recursive: bool = False) -> int: ...

    def apply(self, operation: Operation, *, payload: bytes | None = None) -> None:
        """Perform one operation. Callers must have durably journalled the intent first.

        Raises:
            VaultError: on collisions and on any path escaping the vault root.
        """
        ...

    def stash(self, rel: PurePosixPath) -> str | None:
        """Back up a path and return its key, or ``None`` if absent."""
        ...

    def unstash(self, backup_ref: str) -> bytes:
        """Fetch stashed content. Raises VaultError if the attic entry is gone."""
        ...

    def unique_target(self, folder: PurePosixPath, filename: str) -> PurePosixPath:
        """Return a collision-free target path for a filename."""
        ...
