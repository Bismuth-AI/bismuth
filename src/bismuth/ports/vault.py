"""The filesystem boundary. Paths crossing it are vault-relative :class:`PurePosixPath`, even on Windows."""

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
        """Absolute path of the vault root. The only absolute path in the system."""
        ...

    def exists(self, rel: PurePosixPath) -> bool: ...

    def is_dir(self, rel: PurePosixPath) -> bool: ...

    def read_text(self, rel: PurePosixPath) -> str: ...

    def read_bytes(self, rel: PurePosixPath) -> bytes: ...

    def iter_folders(self) -> Iterator[PurePosixPath]:
        """Every folder in the vault, depth-first, excluding ``.bismuth/``; root is an empty path."""
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
        """Copy the current content of ``rel`` into the attic and return its key, or ``None`` if absent."""
        ...

    def unstash(self, backup_ref: str) -> bytes:
        """Fetch stashed content. Raises VaultError if the attic entry is gone."""
        ...

    def unique_target(self, folder: PurePosixPath, filename: str) -> PurePosixPath:
        """A free (case-insensitively) path for ``filename`` in ``folder``, disambiguating if needed."""
        ...
