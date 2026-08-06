"""Measure a vault against SPEC.md 6.2. Reads only; calls no model; changes nothing.

The targets in the spec are all "미정" because nothing has ever been measured. This
prints the numbers you would fill them in from.

    python scripts/measure_vault.py [VAULT_PATH]

Uses Bismuth's own definition of a document, so "how many documents" means here what
it means everywhere else -- sidecars and folder notes are not documents.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path, PurePosixPath

from bismuth.adapters.vault import FileSystemVault
from bismuth.config import Settings
from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.errors import BismuthError
from bismuth.ports.vault import INBOX


def main(argv: list[str]) -> int:
    root = Path(argv[1]).expanduser() if len(argv) > 1 else Settings().vault_path
    if not root.is_dir():
        print(f"볼트가 없습니다: {root}")
        return 1

    vault = FileSystemVault(root)
    folders = [f for f in vault.iter_folders() if not _in_inbox(f)]
    counts = {f: vault.count_files(f, recursive=False) for f in folders}

    total = sum(counts.values())
    inbox = vault.count_files(INBOX, recursive=True)
    at_root = counts.get(PurePosixPath(), 0)
    named = [f for f in folders if f.parts]

    # A leaf is a folder with no sub-folders: where documents actually end up, and the
    # thing SPEC 3.3 says must stay small enough to look through.
    children = {f: [c for c in named if c.parent == f] for f in folders}
    leaves = [f for f in named if not children[f]]
    leaf_counts = sorted((counts[f] for f in leaves), reverse=True)
    branching = sorted((len(children[f]) for f in folders), reverse=True)
    depths = [len(f.parts) for f in named]

    print(f"볼트: {root}")
    print()
    print(f"  정리된 문서      {total}개  (그중 {at_root}개는 아직 루트)")
    print(f"  인박스           {inbox}개  (위 숫자에 안 들어감)")
    print(f"  폴더            {len(named)}개")
    print(f"  깊이            최대 {max(depths, default=0)}층")
    print()

    print("SPEC 6.2")
    if leaf_counts:
        print(
            f"  잎 폴더 크기     중앙값 {statistics.median(leaf_counts):.0f}"
            f" · 최대 {leaf_counts[0]}"
            f" · 상위 {', '.join(str(n) for n in leaf_counts[:5])}"
        )
    else:
        print("  잎 폴더 크기     (아직 폴더 없음)")
    print(f"  층당 분기 수     최대 {branching[0] if branching else 0}")
    share = f"{at_root / total:.0%}" if total else "-"
    print(f"  아직 안 나뉨     루트에 {at_root} / {total}  ({share})")
    print(f"  미분류           인박스 {inbox}  ← 읽지 못한 문서만 있어야 함")
    print()

    divided = _divided(vault, folders)
    if divided:
        print("나뉜 기록 (SPEC 3.4 / subdivision 5.1)")
        for folder, charter in divided:
            now = vault.count_files(folder, recursive=True)  # matches what was recorded
            due = "  ← 다시 볼 때" if charter.due_for_review(now) else ""
            print(
                f"  {str(folder) or '(루트)':<28} {charter.split_basis!r}"
                f"  {charter.split_at_documents}건일 때 → 지금 {now}건{due}"
            )
        print()

    if named:
        print("트리")
        for folder in sorted(named, key=lambda f: f.parts):
            bar = "·" * min(counts[folder], 40)
            print(f"  {'  ' * (len(folder.parts) - 1)}{folder.name:<24} {counts[folder]:>4} {bar}")
    return 0


def _in_inbox(folder: PurePosixPath) -> bool:
    return bool(folder.parts) and folder.parts[0] == INBOX.parts[0]


def _divided(
    vault: FileSystemVault, folders: list[PurePosixPath]
) -> list[tuple[PurePosixPath, Charter]]:
    found: list[tuple[PurePosixPath, Charter]] = []
    for folder in folders:
        note = folder / CHARTER_FILENAME
        if not vault.exists(note):
            continue
        try:
            charter = Charter.from_markdown(vault.read_text(note), path=folder)
        except BismuthError:
            continue
        if charter.divided:
            found.append((folder, charter))
    return found


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
