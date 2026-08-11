"""Compare two ingest orders by which documents ended up together.

Folder names are deliberately ignored: two librarians may call the same useful class
different things.  Document bytes identify a book, and pairs sharing a direct folder
identify the grouping, matching SPEC.md 6.2.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

IGNORED = {".bismuth", "_inbox"}


@dataclass(frozen=True, slots=True)
class Score:
    together_left: int
    together_right: int
    together_both: int

    @property
    def precision(self) -> float:
        return self.together_both / self.together_left if self.together_left else 1.0

    @property
    def recall(self) -> float:
        return self.together_both / self.together_right if self.together_right else 1.0

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0


def _documents(root: Path) -> dict[str, str]:
    documents: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.name == "_folder.md":
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED for part in relative.parts):
            continue
        # A sidecar is ``original.ext.md`` beside ``original.ext``. A real Markdown
        # document has no such sibling and remains part of the comparison.
        if path.suffix == ".md" and path.with_suffix("").is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        documents[digest] = str(relative.parent)
    return documents


def compare(left: Path, right: Path) -> Score:
    left_docs, right_docs = _documents(left), _documents(right)
    if left_docs.keys() != right_docs.keys():
        missing = len(left_docs.keys() - right_docs.keys())
        extra = len(right_docs.keys() - left_docs.keys())
        raise ValueError(
            f"the vaults do not contain the same documents (missing={missing}, extra={extra})"
        )

    together_left = together_right = together_both = 0
    ids = sorted(left_docs)
    for index, first in enumerate(ids):
        for second in ids[index + 1 :]:
            same_left = left_docs[first] == left_docs[second]
            same_right = right_docs[first] == right_docs[second]
            together_left += same_left
            together_right += same_right
            together_both += same_left and same_right
    return Score(together_left, together_right, together_both)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: compare_vaults.py VAULT_FROM_ORDER_A VAULT_FROM_ORDER_B")
        return 2
    try:
        score = compare(Path(argv[1]), Path(argv[2]))
    except ValueError as exc:
        print(exc)
        return 1
    print(f"same-folder pair precision: {score.precision:.3f}")
    print(f"same-folder pair recall:    {score.recall:.3f}")
    print(f"same-folder pair F1:        {score.f1:.3f}")
    print(
        f"pairs: left={score.together_left}, right={score.together_right}, "
        f"intersection={score.together_both}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
