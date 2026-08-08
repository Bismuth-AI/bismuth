"""Path sanitization/safety, and the placement service's three refusal paths."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.domain.document import DocumentCard
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.placement import Verdict
from bismuth.prompts import placement as placement_prompts
from bismuth.services.placement import PlacementService, _safe_path


class TestSanitizeSegment:
    def test_keeps_korean(self) -> None:
        assert sanitize_segment("영업본부") == "영업본부"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Q1/Q2 2023", "Q1 Q2 2023"),  # a slash must not become a directory level
            ("a<b>c", "a b c"),
            ('say "hi"', "say hi"),
            ("trailing.  ", "trailing"),
        ],
    )
    def test_strips_dangerous_characters(self, raw: str, expected: str) -> None:
        assert sanitize_segment(raw) == expected

    @pytest.mark.parametrize("reserved", ["CON", "prn", "com1", "LPT9"])
    def test_escapes_windows_device_names(self, reserved: str) -> None:
        assert sanitize_segment(reserved) == f"{reserved}_"

    def test_strips_leading_underscore(self) -> None:
        # _inbox and _folder.md are Bismuth's namespace; a folder name must not collide.
        assert sanitize_segment("_inbox") == "inbox"

    @pytest.mark.parametrize("hostile", ["...", "   ", "///"])
    def test_rejects_the_unusable(self, hostile: str) -> None:
        with pytest.raises(ValueError):
            sanitize_segment(hostile)


class TestSafePath:
    def test_a_normal_path_passes(self) -> None:
        assert _safe_path("법무/계약/2023") == PurePosixPath("법무/계약/2023")

    def test_traversal_is_stripped(self) -> None:
        assert _safe_path("../../etc/passwd") == PurePosixPath("etc/passwd")

    def test_backslashes_are_treated_as_separators(self) -> None:
        assert _safe_path("a\\b") == PurePosixPath("a/b")

    def test_empty_segments_collapse(self) -> None:
        assert _safe_path("a///b/") == PurePosixPath("a/b")

    def test_a_path_of_nothing_usable_is_none(self) -> None:
        assert _safe_path("...///...") is None


def _card() -> DocumentCard:
    return DocumentCard(title="t", summary="s", doc_type="문서", topics=("x",))


def _decision(folder: str | None, *, confidence: float = 0.9, existing: bool = False):
    return placement_prompts.PlacementDecision(
        folder=folder, existing=existing, confidence=confidence, reason="r"
    )


async def _decide(decision, *, exists: str | None = None, **kwargs):
    """Decide against a tree that holds `exists`, if anything."""
    llm = FakeLLM(queue=[decision])
    folders = [(exists, "기존")] if exists else []
    return await PlacementService(llm).decide(
        document_id="doc1",
        card=_card(),
        folders=folders,
        existing_paths=frozenset({exists} if exists else set()),
        **kwargs,
    )


class TestPlacementService:
    """There is no confidence threshold: not knowing where a document goes is answered
    by the root, which is what "no distinction drawn yet" means (SPEC.md 3.4)."""

    async def test_a_folder_is_placed(self) -> None:
        placement = await _decide(_decision("아폴로/2023"), exists="아폴로/2023")
        assert placement.verdict is Verdict.PLACED
        assert placement.target == PurePosixPath("아폴로/2023")
        assert placement.created_folder is False  # nothing here creates one

    async def test_a_folder_that_does_not_exist_is_read_as_the_root(self) -> None:
        """Placement answers "where in the tree as it stands". A folder that is not in
        the tree is not an answer it can give, and the root is (SPEC.md 3.4). Measured
        without this: twenty invented folders, each named after a single document and
        each becoming the precedent for the next."""
        placement = await _decide(_decision("아폴로/2023"))

        assert placement.verdict is Verdict.PLACED
        assert placement.target == PurePosixPath()
        assert placement.created_folder is False

    async def test_the_empty_string_means_the_root(self) -> None:
        placement = await _decide(_decision(""))

        assert placement.verdict is Verdict.PLACED
        assert placement.target == PurePosixPath()
        # The root is not created by anybody; it is where things start.
        assert placement.created_folder is False

    async def test_null_is_the_only_road_to_the_inbox(self) -> None:
        """Reserved for documents that could not be read at all."""
        placement = await _decide(_decision(None))
        assert placement.verdict is Verdict.INBOX

    async def test_low_confidence_still_files_the_document(self) -> None:
        placement = await _decide(_decision("아폴로/2023", confidence=0.02), exists="아폴로/2023")

        assert placement.verdict is Verdict.PLACED
        assert placement.target == PurePosixPath("아폴로/2023")

    async def test_the_number_is_recorded_even_though_it_gates_nothing(self) -> None:
        placement = await _decide(_decision("아폴로/2023", confidence=0.42), exists="아폴로/2023")
        assert placement.confidence == pytest.approx(0.42)

    async def test_an_unusable_path_falls_back_to_the_root(self) -> None:
        placement = await _decide(_decision("...///..."))

        assert placement.verdict is Verdict.PLACED
        assert placement.target == PurePosixPath()

    async def test_the_model_saying_existing_does_not_make_it_so(self) -> None:
        """Decided against the real folder set, never the model's own `existing` flag."""
        placement = await _decide(_decision("없는/폴더", existing=True), exists="아폴로/2023")

        assert placement.target == PurePosixPath()
