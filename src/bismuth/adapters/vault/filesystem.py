"""Filesystem-backed vault with atomic writes and path validation."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from bismuth.domain.charter import CHARTER_FILENAME
from bismuth.domain.document import sidecar_name
from bismuth.domain.errors import VaultError
from bismuth.domain.journal import Operation, OperationKind
from bismuth.ports.vault import INBOX, STATE_DIR

_ATTIC = "attic"
_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


def _plain(path: Path) -> Path:
    """Drop Windows' extended-length prefix so a path can be compared with a plain root."""
    text = str(path)
    if text.startswith(_EXTENDED_UNC):
        return Path("\\\\" + text[len(_EXTENDED_UNC) :])
    if text.startswith(_EXTENDED):
        return Path(text[len(_EXTENDED) :])
    return path


class FileSystemVault:
    """A directory Bismuth organises."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / STATE_DIR / _ATTIC).mkdir(parents=True, exist_ok=True)
        (self._root / INBOX).mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, rel: PurePosixPath) -> Path:
        """Vault-relative to absolute, refusing any resolved path that escapes the vault (catches ``../`` and symlink escapes)."""
        candidate = (self._root / Path(*rel.parts)).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise VaultError(f"path escapes the vault: {rel}")
        return candidate

    def relative(self, absolute: Path) -> PurePosixPath:
        """Convert an absolute path to a vault-relative path."""
        try:
            return PurePosixPath(absolute.relative_to(self._root).as_posix())
        except ValueError:
            pass
        return PurePosixPath(_plain(absolute.resolve()).relative_to(self._root).as_posix())

    def exists(self, rel: PurePosixPath) -> bool:
        return self.resolve(rel).exists()

    def is_dir(self, rel: PurePosixPath) -> bool:
        return self.resolve(rel).is_dir()

    def read_text(self, rel: PurePosixPath) -> str:
        try:
            return self.resolve(rel).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise VaultError(f"no such file in vault: {rel}") from exc

    def read_bytes(self, rel: PurePosixPath) -> bytes:
        try:
            return self.resolve(rel).read_bytes()
        except FileNotFoundError as exc:
            raise VaultError(f"no such file in vault: {rel}") from exc

    def iter_folders(self) -> Iterator[PurePosixPath]:
        yield PurePosixPath()
        for dirpath, dirnames, _ in os.walk(self._root):
            dirnames[:] = sorted(d for d in dirnames if not _is_private(d))
            for name in dirnames:
                yield self.relative(Path(dirpath) / name)

    def iter_files(self, rel: PurePosixPath, *, recursive: bool = False) -> Iterator[PurePosixPath]:
        base = self.resolve(rel)
        if not base.is_dir():
            return

        if recursive:
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(d for d in dirnames if not _is_private(d))
                for name in sorted(filenames):
                    path = Path(dirpath) / name
                    if not self._is_own_artefact(path):
                        yield self.relative(path)
        else:
            for path in sorted(base.iterdir()):
                if path.is_file() and not self._is_own_artefact(path):
                    yield self.relative(path)

    def count_files(self, rel: PurePosixPath, *, recursive: bool = False) -> int:
        return sum(1 for _ in self.iter_files(rel, recursive=recursive))

    def _is_own_artefact(self, path: Path) -> bool:
        """Return whether a path is a generated sidecar or folder charter."""
        name = path.name
        if name == CHARTER_FILENAME:
            return True
        if not name.endswith(".md"):
            return False
        # A sidecar name alone isn't enough; the document must exist too, or a
        # genuine notes.v2.md would be misdetected as a sidecar.
        stem = name[: -len(".md")]
        return bool(stem) and sidecar_name(stem) == name and (path.parent / stem).exists()

    def apply(self, operation: Operation, *, payload: bytes | None = None) -> None:
        target = self.resolve(operation.target)

        match operation.kind:
            case OperationKind.MKDIR:
                target.mkdir(parents=True, exist_ok=True)

            case OperationKind.MOVE:
                if operation.source is None:
                    raise VaultError("MOVE without a source")
                source = self.resolve(operation.source)
                if not source.exists():
                    raise VaultError(f"cannot move missing file: {operation.source}")
                if target.exists() and not _same_file(source, target):
                    raise VaultError(
                        f"refusing to overwrite {operation.target} with {operation.source}. "
                        f"Collisions are resolved before journalling, never here."
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))

            case OperationKind.WRITE:
                if payload is None:
                    raise VaultError(f"WRITE to {operation.target} without payload")
                _atomic_write(target, payload)

            case OperationKind.RESTORE:
                if operation.backup_ref is None:
                    raise VaultError("RESTORE without a backup_ref")
                _atomic_write(target, self.unstash(operation.backup_ref))

            case OperationKind.REMOVE:
                target.unlink(missing_ok=True)

            case OperationKind.RMDIR:
                # Never recursive: a non-empty folder stays, so rollback can't
                # delete files the user has since added.
                if target.is_dir() and not any(target.iterdir()):
                    target.rmdir()

    def stash(self, rel: PurePosixPath) -> str | None:
        source = self.resolve(rel)
        if not source.is_file():
            return None
        data = source.read_bytes()
        key = hashlib.sha256(data).hexdigest()[:32]
        destination = self._root / STATE_DIR / _ATTIC / key
        if not destination.exists():
            _atomic_write(destination, data)
        return key

    def unstash(self, backup_ref: str) -> bytes:
        path = self._root / STATE_DIR / _ATTIC / backup_ref
        if not path.is_file():
            raise VaultError(
                f"attic entry {backup_ref} is gone -- this undo cannot be completed. "
                f"(Did something prune {STATE_DIR}/{_ATTIC}/?)"
            )
        return path.read_bytes()

    def unique_target(self, folder: PurePosixPath, filename: str) -> PurePosixPath:
        """Return a case-insensitively unique filename in ``folder``."""
        taken = (
            {p.name.casefold() for p in self.resolve(folder).iterdir()}
            if self.exists(folder)
            else set()
        )
        if filename.casefold() not in taken:
            return folder / filename

        stem, dot, extension = filename.rpartition(".")
        stem, extension = (stem, f".{extension}") if dot else (filename, "")
        for index in range(2, 1000):
            candidate = f"{stem} ({index}){extension}"
            if candidate.casefold() not in taken:
                return folder / candidate
        raise VaultError(f"cannot find a free name for {filename} in {folder}")


def _is_private(dirname: str) -> bool:
    return dirname == STATE_DIR or dirname in {".git", ".svn", "__pycache__", "node_modules"}


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


def _atomic_write(target: Path, payload: bytes) -> None:
    """Write through a same-directory temporary file and atomically replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".bismuth-tmp-")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
