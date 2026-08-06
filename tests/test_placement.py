"""Path sanitization/safety, and the placement service's three refusal paths."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.config import Settings
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


def _decision(folder: str | None, *, confidence: float = 0.9):
    return placement_prompts.PlacementDecision(
        folder=folder, existing=False, confidence=confidence, reason="r"
    )


async def _decide(decision, **kwargs):
    llm = FakeLLM(queue=[decision])
    service = PlacementService(llm, min_confidence=0.55)
    return await service.decide(
        document_id="doc1", card=_card(), folders=[], existing_paths=frozenset(), **kwargs
    )


class TestPlacementService:
    async def test_a_confident_folder_is_placed(self) -> None:
        placement = await _decide(_decision("아폴로/2023"))
        assert placement.verdict is Verdict.PLACED
        assert placement.target == PurePosixPath("아폴로/2023")

    async def test_null_declines_to_the_inbox(self) -> None:
        placement = await _decide(_decision(None))
        assert placement.verdict is Verdict.INBOX

    async def test_low_confidence_declines(self) -> None:
        placement = await _decide(_decision("아폴로/2023", confidence=0.2))
        assert placement.verdict is Verdict.INBOX

    async def test_an_unusable_path_declines(self) -> None:
        placement = await _decide(_decision("...///..."))
        assert placement.verdict is Verdict.INBOX

    async def test_a_parked_document_keeps_the_number_it_was_parked_for(self) -> None:
        """Tuning the threshold needs the figure as a figure, not inside a Korean sentence."""
        placement = await _decide(_decision("환경/생태계", confidence=0.42))

        assert placement.verdict is Verdict.INBOX
        assert placement.confidence == pytest.approx(0.42)
        assert "42%" in placement.rationale

    async def test_a_parked_document_keeps_the_folder_the_model_wanted(self) -> None:
        """The user re-deciding this by hand should not have to start from nothing."""
        placement = await _decide(_decision("환경/생태계", confidence=0.42))
        assert placement.suggested == PurePosixPath("환경/생태계")

    async def test_the_suggestion_is_sanitised_like_a_real_target(self) -> None:
        placement = await _decide(_decision("환경/../생태계", confidence=0.1))
        assert placement.suggested == PurePosixPath("환경/생태계")

    async def test_an_undecidable_document_has_no_suggestion(self) -> None:
        placement = await _decide(_decision(None, confidence=0.3))
        assert placement.suggested is None
        assert placement.confidence == pytest.approx(0.3)

    async def test_a_placed_document_has_no_suggestion_to_make(self) -> None:
        placement = await _decide(_decision("아폴로/2023"))
        assert placement.suggested is None
        assert placement.confidence == pytest.approx(0.9)

    async def test_the_default_threshold_matches_the_one_the_app_ships(self) -> None:
        # The two drifting apart means direct callers are not testing shipped behaviour.
        assert (
            PlacementService(FakeLLM(queue=[]))._min_confidence
            == Settings().placement_min_confidence
        )

    async def test_created_folder_is_flagged_against_the_existing_set(self) -> None:
        # Decided against the real folder set, not the model's own `existing` flag.
        llm = FakeLLM(queue=[_decision("아폴로/2023")])
        service = PlacementService(llm)
        placement = await service.decide(
            document_id="d",
            card=_card(),
            folders=[("아폴로/2023", "기존")],
            existing_paths=frozenset({"아폴로/2023"}),
        )
        assert placement.created_folder is False
