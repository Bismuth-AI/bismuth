"""The cataloguing loop: whole-document reading, union of facts, honest coverage."""

from __future__ import annotations

import json

import pytest

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.domain.document import Entity, EntityKind, Extraction, Section
from bismuth.domain.errors import StructuredOutputError
from bismuth.ports.llm import Prompt
from bismuth.prompts import cards as card_prompts
from bismuth.services.cards import (
    LABEL_MAX_CHARS,
    NAME_MAX_CHARS,
    CardService,
)

from .conftest import ScriptedModel


def _reads(llm: FakeLLM, *, later: bool = False) -> list[Prompt]:
    """The prompts that asked a window to be read.

    Reading is open text now (no schema to key on), so the two kinds are told apart by
    the contract each one carries: only the update contract makes SUMMARY compulsory.
    """
    return [
        prompt
        for prompt, schema in llm.calls
        if schema is None
        and "Return plain tagged lines" in prompt.system
        and ("SUMMARY is required" in prompt.system) is later
    ]


def _extraction(text: str, *, truncated: bool = False) -> Extraction:
    return Extraction(sections=(Section(text=text, order=0),), parser="test", truncated=truncated)


def _long(chars: int) -> str:
    """Text with no headings, no blank lines, no structure to lean on."""
    return "가나다라마바사아자차카타파하" * (chars // 14 + 1)


class TestWindows:
    def test_short_document_is_one_window(self) -> None:
        windows = _extraction("짧은 문서").windows(100)
        assert len(windows) == 1
        assert windows[0].text == "짧은 문서"
        assert (windows[0].start, windows[0].end) == (0, 5)

    def test_windows_tile_the_whole_text_without_gaps(self) -> None:
        extraction = _extraction(_long(1000))
        windows = extraction.windows(100)

        assert windows[0].start == 0
        assert windows[-1].end == len(extraction.text)
        assert [w.end for w in windows[:-1]] == [w.start for w in windows[1:]]
        assert "".join(w.text for w in windows) == extraction.text

    def test_cut_snaps_back_to_a_line_break_when_one_is_near(self) -> None:
        text = "a" * 90 + "\n" + "b" * 100
        windows = _extraction(text).windows(100)
        assert windows[0].text.endswith("\n")
        assert windows[1].text.startswith("b")

    def test_empty_extraction_has_no_windows(self) -> None:
        assert _extraction("").windows(100) == ()


class TestOneLineSeveralItems:
    """A comma-separated response still represents several labels."""

    def test_a_comma_separated_line_is_several_items(self) -> None:
        card = card_prompts.parse_card(
            "SUMMARY: 한 문장.\nKEYWORD: 온누리상품권, 가맹점, 과징금\nTOPIC: 전통시장"
        )

        assert card.keywords == ("온누리상품권", "가맹점", "과징금")
        assert card.topics == ("전통시장",)

    def test_a_label_that_contains_a_comma_survives(self) -> None:
        """Only a separator, never a rewrite: splitting must not leave an empty piece."""
        card = card_prompts.parse_card("SUMMARY: 한 문장.\nTOPIC: 대ㆍ중소기업 상생협력,")

        assert card.topics == ("대ㆍ중소기업 상생협력,",)


class TestDescribe:
    async def test_short_document_costs_one_call(self, llm: FakeLLM, script: ScriptedModel) -> None:
        card = await CardService(llm, context_chars=10_000).describe(
            _extraction("아폴로 계약서"), filename="계약.pdf"
        )

        assert llm.call_count == 1
        assert card.title == "아폴로 지원 계약서"
        assert card.coverage is not None
        assert card.coverage.whole_document

    async def test_long_document_is_read_to_the_end(self, llm: FakeLLM) -> None:
        extraction = _extraction(_long(1000))
        card = await CardService(llm, context_chars=100).describe(extraction, filename="긴문서.pdf")

        coverage = card.coverage
        assert coverage is not None
        assert coverage.windows_total > 5
        assert coverage.windows_read == coverage.windows_total
        assert coverage.chars_read == coverage.chars_total == len(extraction.text)
        assert coverage.whole_document

    async def test_facts_accumulate_as_a_union_without_duplicates(self, llm: FakeLLM) -> None:
        card = await CardService(llm, context_chars=100).describe(
            _extraction(_long(500)), filename="긴문서.pdf"
        )

        # Every scripted update offers the same facts; the card keeps one of each.
        assert card.topics.count("지연배상") == 1
        assert [e.name for e in card.entities].count("대한물산") == 1
        # ...alongside what the first window found.
        assert "아폴로" in card.topics

    async def test_final_pass_rewrites_the_summary_from_the_gathered_facts(
        self, llm: FakeLLM
    ) -> None:
        card = await CardService(llm, context_chars=100).describe(
            _extraction(_long(500)), filename="긴문서.pdf"
        )

        assert (
            card.summary
            == "대한물산과 유엔진 간 아폴로 유지보수 계약. 기간 24개월, 지연배상 조항 포함."
        )
        assert llm.prompts_for(card_prompts.DensifiedSummary)

    async def test_no_densify_pass_for_a_single_window(self, llm: FakeLLM) -> None:
        await CardService(llm, context_chars=10_000).describe(
            _extraction("짧다"), filename="짧은.pdf"
        )
        assert not llm.prompts_for(card_prompts.DensifiedSummary)

    async def test_over_budget_documents_are_sampled_across_their_length(
        self, llm: FakeLLM
    ) -> None:
        extraction = _extraction(_long(2000))
        card = await CardService(llm, context_chars=100, max_windows=4).describe(
            extraction, filename="아주긴문서.pdf"
        )

        coverage = card.coverage
        assert coverage is not None
        assert coverage.windows_read == 4 < coverage.windows_total
        assert not coverage.whole_document

        # The point of striding: the end of the document is read, not just the top.
        read = [p.user for p in _reads(llm, later=True)]
        last = coverage.windows_total
        assert f"This section is {last}/{last}." in read[-1]
        assert f"This section is 2/{last}." not in read[0]

    async def test_a_failed_window_keeps_the_card_built_so_far(self, llm: FakeLLM) -> None:
        calls = {"n": 0}
        script = ScriptedModel()

        def flaky(prompt, schema):  # type: ignore[no-untyped-def]
            if "SUMMARY is required" in prompt.system:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise StructuredOutputError("scripted failure")
            return script(prompt, schema)

        card = await CardService(FakeLLM(handler=flaky), context_chars=100).describe(
            _extraction(_long(500)), filename="깨진문서.pdf"
        )

        assert card.title == "아폴로 지원 계약서"
        assert card.coverage is not None
        assert card.coverage.windows_failed == 1

    async def test_a_complete_document_is_not_announced_as_cut_off(self, llm: FakeLLM) -> None:
        await CardService(llm, context_chars=10_000).describe(
            _extraction("짧고 온전한 문서"), filename="짧은.pdf"
        )
        sent = _reads(llm)[0].user
        assert "참고:" not in sent

    async def test_a_cut_off_document_says_so_in_the_prompt(self, llm: FakeLLM) -> None:
        await CardService(llm, context_chars=10_000).describe(
            _extraction("잘린 문서", truncated=True), filename="잘린.pdf"
        )
        assert "Extraction stopped before the end of the file" in _reads(llm)[0].user

    async def test_later_windows_are_announced_as_parts(self, llm: FakeLLM) -> None:
        await CardService(llm, context_chars=100).describe(
            _extraction(_long(500)), filename="긴문서.pdf"
        )
        assert "This is the first of" in _reads(llm)[0].user

    async def test_extraction_truncation_is_still_reported(self, llm: FakeLLM) -> None:
        card = await CardService(llm, context_chars=10_000).describe(
            _extraction("잘린 문서", truncated=True), filename="잘린.pdf"
        )
        assert card.coverage is not None
        assert not card.coverage.whole_document
        assert card.coverage.extraction_truncated


class TestLabelHygiene:
    """Labels that cannot serve as compact metadata are rejected."""

    async def test_an_entry_too_long_to_be_a_label_is_dropped(self, script: ScriptedModel) -> None:
        # Bypass schema validation to exercise the service boundary.
        script.set(
            card_prompts.CardDraft,
            card_prompts.CardDraft.model_construct(
                title="논문",
                summary="요약",
                doc_type="학술논문",
                language="ko",
                topics=["생태계서비스", "참" * (LABEL_MAX_CHARS + 1)],
                entities=[
                    Entity(name="환경부", kind=EntityKind.ORGANIZATION),
                    Entity(name="A" * (NAME_MAX_CHARS + 1), kind=EntityKind.PERSON),
                ],
                keywords=["ESV", "가" * (LABEL_MAX_CHARS + 1)],
                answers_questions=[],
            ),
        )
        card = await CardService(FakeLLM(handler=script), context_chars=10_000).describe(
            _extraction("논문 본문"), filename="논문.pdf"
        )

        assert card.topics == ("생태계서비스",)
        assert [e.name for e in card.entities] == ["환경부"]
        assert card.keywords == ("ESV",)

    async def test_a_label_at_the_limit_survives(self, script: ScriptedModel) -> None:
        exact = "가" * LABEL_MAX_CHARS
        script.set(
            card_prompts.CardDraft,
            card_prompts.CardDraft(
                title="t", summary="s", doc_type="문서", language="ko", topics=[exact]
            ),
        )
        card = await CardService(FakeLLM(handler=script), context_chars=10_000).describe(
            _extraction("본문"), filename="t.pdf"
        )
        assert card.topics == (exact,)

    async def test_what_was_dropped_is_recorded_rather_than_swallowed(
        self, script: ScriptedModel, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        from bismuth.logging_setup import configure_logging

        logs = configure_logging(log_dir=tmp_path / "logs")
        script.set(
            card_prompts.CardDraft,
            card_prompts.CardDraft.model_construct(
                title="t",
                summary="s",
                doc_type="문서",
                language="ko",
                topics=["참" * 200],
                entities=[],
                keywords=[],
                answers_questions=[],
            ),
        )
        await CardService(FakeLLM(handler=script), context_chars=10_000).describe(
            _extraction("본문"), filename="t.pdf", document_id="abc"
        )

        lines = [
            json.loads(line)
            for line in (logs / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        rejected = next(line for line in lines if line["event"] == "card.rejected")
        assert rejected["rejected"]["topics"] == ["참" * 200]
        assert rejected["document_id"] == "abc"


class TestTrace:
    @pytest.fixture(autouse=True)
    def _logs(self, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
        from bismuth.logging_setup import configure_logging

        return configure_logging(log_dir=tmp_path / "logs")

    async def test_every_window_leaves_a_replayable_line(self, llm: FakeLLM, tmp_path) -> None:
        await CardService(llm, context_chars=100).describe(
            _extraction(_long(500)), filename="긴문서.pdf", document_id="abc123"
        )

        lines = [
            json.loads(line)
            for line in (tmp_path / "logs" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        events = [line["event"] for line in lines]

        assert events[0] == "card.begin"
        assert events[-1] == "card.done"
        assert every_line_names_the_document(lines)

        windows = [line for line in lines if line["event"] == "card.window"]
        assert windows[0]["pass_kind"] == "first"
        assert all("chars" in w and "text_head" in w and "added" in w for w in windows)

        done = lines[-1]
        assert done["coverage"]["windows_read"] == len(windows)
        assert done["card"]["title"] == "아폴로 지원 계약서"


def every_line_names_the_document(lines: list[dict]) -> bool:
    """Filtering trace.jsonl by document_id must yield the whole story for that document."""
    return all(line.get("document_id") == "abc123" for line in lines)


def test_entity_dedup_is_case_and_kind_aware() -> None:
    a = Entity(name="대한물산", kind=EntityKind.ORGANIZATION)
    b = Entity(name="  대한물산 ", kind=EntityKind.ORGANIZATION)
    c = Entity(name="대한물산", kind=EntityKind.PRODUCT)
    assert a.key() == b.key() != c.key()
