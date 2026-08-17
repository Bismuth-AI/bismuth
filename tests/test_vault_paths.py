"""Turning an absolute path back into a vault-relative one.

/status walks the vault while an ingest is filing into it. A document seen by the walk
can be gone by the time anything looks at it again, and on Windows resolving a path in
that state comes back in the extended-length form -- which no longer compares against a
plain root. One live run raised

    ValueError: '\\\\?\\C:\\Users\\<user>\\bismuth-vault\\...\\<document>.pdf'
    is not in the subpath of 'C:\\Users\\<user>\\bismuth-vault'
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import pytest

from bismuth.adapters.vault.filesystem import FileSystemVault, _plain


def test_a_path_under_the_root_needs_no_second_look_at_the_disk(tmp_path: Path) -> None:
    vault = FileSystemVault(tmp_path / "vault")
    gone = vault.root / "폴더" / "사라진 문서.pdf"

    # Never created, so resolving it is exactly the state the live failure was in.
    assert vault.relative(gone) == PurePosixPath("폴더/사라진 문서.pdf")


@pytest.mark.skipif(sys.platform != "win32", reason="the extended-length form is Windows-only")
def test_the_extended_length_form_still_lands_inside_the_vault(tmp_path: Path) -> None:
    vault = FileSystemVault(tmp_path / "vault")
    extended = Path("\\\\?\\" + str(vault.root / "폴더" / "문서.pdf"))

    assert vault.relative(extended) == PurePosixPath("폴더/문서.pdf")


def test_plain_drops_the_prefix_and_leaves_ordinary_paths_alone() -> None:
    assert str(_plain(Path("\\\\?\\C:\\vault\\x.pdf"))) == "C:\\vault\\x.pdf"
    assert str(_plain(Path("\\\\?\\UNC\\server\\share\\x.pdf"))) == "\\\\server\\share\\x.pdf"
    assert str(_plain(Path("C:\\vault\\x.pdf"))) == "C:\\vault\\x.pdf"
