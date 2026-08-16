"""Score a vault against SPEC.md 3.3.1 and 6.2. Reads only; calls no model; changes nothing.

3.3.1 states the shape an archive of a given size should have, derived from what an
agent with ls/grep/read can afford. This scores a vault against it, so one round can be
compared to the next without another model call -- the baseline a benchmark needs.

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

    print(_scorecard(total, leaf_counts, branching, depths, folders, children, counts))
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


#: SPEC 3.3.1 -- the band a well-shaped archive of this size sits in, as
#: (documents, widest branching, deepest level, largest leaf). Interpolation between
#: rows is deliberate: the shape moves with size rather than snapping between tiers.
_BANDS = (
    (100, 3, 5, 2, 20),
    (1_000, 5, 9, 3, 25),
    (10_000, 6, 10, 4, 30),
    (100_000, 8, 12, 5, 30),
)
#: Absolute ceilings from the same section. Past any of these the branch has failed.
_CEILING = (20, 5, 50)


def _band(total: int) -> tuple[int, int, int, int]:
    """The width band, depth and leaf ceiling recommended at this archive size.

    Width is a band, not a ceiling. Read as a ceiling alone it scores an archive that was
    never divided as a pass: 300 documents in 5 folders, one leaf of 198, width 2 -- four
    of five rows green. Too few names at a level is the same failure as too many.
    """
    for size, floor, width, depth, leaf in _BANDS:
        if total <= size:
            return floor, width, depth, leaf
    return _BANDS[-1][1:]


def _pass_through(folders, children, counts) -> list[PurePosixPath]:
    """Folders that cost an ls and narrow nothing: almost no documents, one child."""
    return [
        folder
        for folder in folders
        if folder.parts and len(children[folder]) == 1 and counts[folder] < 2
    ]


def _undivided(folders, children, counts) -> list[PurePosixPath]:
    """Folders whose loose pile outweighs their largest child -- the split did not happen."""
    return [
        folder
        for folder in folders
        if children[folder] and counts[folder] > max(counts[c] for c in children[folder])
    ]


def _line(label: str, value: str, ok: bool | None) -> str:
    mark = "  " if ok is None else (" ✓" if ok else " ✗")
    return f"  {label:<14} {value:<34}{mark}"


def _scorecard(total, leaf_counts, branching, depths, folders, children, counts) -> str:
    """How this vault sits against the recommended shape for its size (SPEC 3.3.1)."""
    width_min, width_max, depth_max, leaf_max = _band(total)
    width_cap, depth_cap, leaf_cap = _CEILING
    width = branching[0] if branching else 0
    depth = max(depths, default=0)
    leaf = leaf_counts[0] if leaf_counts else 0
    through = _pass_through(folders, children, counts)
    undivided = _undivided(folders, children, counts)

    rows = [
        f"SPEC 3.3.1 — {total}건 장서의 권장 형태: "
        f"폭 {width_min}~{width_max} · 깊이 ≤{depth_max} · 잎 ≤{leaf_max}",
        "",
        _line(
            "층당 폭",
            f"최대 {width}  (권장 {width_min}~{width_max}, 상한 {width_cap})",
            width_min <= width <= width_max,
        ),
        _line("깊이", f"최대 {depth}  (권장 {depth_max}, 상한 {depth_cap})", depth <= depth_max),
        _line("잎 크기", f"최대 {leaf}  (권장 {leaf_max}, 상한 {leaf_cap})", leaf <= leaf_max),
        _line("통과 폴더", f"{len(through)}개  (목표 0)", not through),
        _line("미분해 더미", f"{len(undivided)}개  (목표 0)", not undivided),
    ]
    for folder in through[:3]:
        rows.append(f"      통과: {folder}")
    for folder in undivided[:3]:
        largest = max(counts[c] for c in children[folder])
        rows.append(f"      더미: {folder}  ({counts[folder]}건 남음 > 최대 자식 {largest}건)")
    return "\n".join(rows) + "\n"


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
