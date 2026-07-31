"""Filesystem adapters."""

from bismuth.adapters.vault.filesystem import FileSystemVault
from bismuth.domain.document import sidecar_name as sidecar_name

__all__ = ["FileSystemVault", "sidecar_name"]
