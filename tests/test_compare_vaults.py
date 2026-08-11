from pathlib import Path

from scripts.compare_vaults import compare


def _put(root: Path, folder: str, name: str, body: str) -> None:
    target = root / folder / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_order_comparison_ignores_folder_names_and_measures_groups(tmp_path: Path) -> None:
    left, right = tmp_path / "left", tmp_path / "right"
    _put(left, "문학", "a.txt", "a")
    _put(left, "문학", "b.txt", "b")
    _put(left, "과학", "c.txt", "c")
    _put(right, "books", "a.txt", "a")
    _put(right, "books", "b.txt", "b")
    _put(right, "science", "c.txt", "c")

    score = compare(left, right)

    assert score.f1 == 1.0


def test_order_comparison_exposes_a_different_grouping(tmp_path: Path) -> None:
    left, right = tmp_path / "left", tmp_path / "right"
    _put(left, "one", "a.txt", "a")
    _put(left, "one", "b.txt", "b")
    _put(left, "two", "c.txt", "c")
    _put(right, "one", "a.txt", "a")
    _put(right, "two", "b.txt", "b")
    _put(right, "two", "c.txt", "c")

    score = compare(left, right)

    assert score.f1 == 0.0
