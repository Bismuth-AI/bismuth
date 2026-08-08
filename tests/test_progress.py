"""Progress reporting: the steps a document goes through, and the channel they travel on."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

from bismuth.api.progress import ProgressBus, as_event, stream
from bismuth.container import Bismuth
from bismuth.domain.document import Extraction, Section
from bismuth.domain.progress import Progress, Stage, report
from bismuth.prompts import placement as placement_prompts
from bismuth.services.cards import CardService

from .conftest import ScriptedModel


def _p(stage: Stage, **kw: object) -> Progress:
    return Progress(stage=stage, filename="계약.pdf", **kw)  # type: ignore[arg-type]


class TestProgressValue:
    def test_reading_says_which_window_and_what_it_found(self) -> None:
        p = _p(Stage.READING, step=3, steps=8, found=("지연배상", "대한물산"))
        assert p.label() == "3/8조각 읽는 중 — 지연배상, 대한물산"
        assert p.fraction == pytest.approx(0.375)
        assert not p.terminal

    def test_a_window_full_of_finds_still_fits_on_one_line(self) -> None:
        """A real run turned up a dozen things in one window; the line is a status, not a list."""
        p = _p(Stage.READING, step=2, steps=5, found=tuple(f"주제{i}" for i in range(9)))
        assert p.label() == "2/5조각 읽는 중 — 주제0, 주제1, 주제2 외 6개"

    def test_exactly_three_finds_need_no_counter(self) -> None:
        p = _p(Stage.READING, step=1, steps=2, found=("가", "나", "다"))
        assert p.label() == "1/2조각 읽는 중 — 가, 나, 다"

    def test_a_step_with_no_measure_has_no_fraction(self) -> None:
        assert _p(Stage.FILING).fraction is None

    def test_placing_into_an_empty_vault_does_not_say_zero_folders(self) -> None:
        assert _p(Stage.PLACING, steps=0).label() == "첫 문서라 둘 폴더를 새로 정하는 중"
        assert "12개" in _p(Stage.PLACING, steps=12).label()

    def test_a_total_without_a_position_is_not_progress(self) -> None:
        # Placement knows how many folders it weighs, not which one it is on. Reporting
        # that as 0% would rewind a bar the reading steps had already filled.
        assert _p(Stage.PLACING, steps=12).fraction is None

    def test_done_duplicate_and_failed_are_terminal(self) -> None:
        assert all(_p(s).terminal for s in (Stage.DONE, Stage.DUPLICATE, Stage.FAILED))
        assert not _p(Stage.PARSING).terminal

    def test_every_stage_has_a_label(self) -> None:
        # A stage added without a label would render as an empty line, which reads as a hang.
        assert all(_p(s, note="x", steps=1, step=1).label() for s in Stage)


class TestReport:
    def test_no_sink_is_fine(self) -> None:
        report(None, _p(Stage.DONE))

    def test_a_broken_listener_does_not_break_the_ingest(self) -> None:
        def explode(_: Progress) -> None:
            raise RuntimeError("the UI is on fire")

        report(explode, _p(Stage.DONE))  # must not raise


class TestBus:
    async def test_a_subscriber_receives_what_was_published(self) -> None:
        bus = ProgressBus()
        with bus.subscribe() as queue:
            bus.publish(_p(Stage.PARSING, note="pypdf"))
            assert (await queue.get()).note == "pypdf"

    async def test_every_subscriber_gets_every_step(self) -> None:
        bus = ProgressBus()
        with bus.subscribe() as one, bus.subscribe() as two:
            bus.publish(_p(Stage.DONE))
            assert (await one.get()).stage is (await two.get()).stage is Stage.DONE

    async def test_leaving_the_context_unsubscribes(self) -> None:
        bus = ProgressBus()
        with bus.subscribe():
            assert bus.watchers == 1
        assert bus.watchers == 0

    async def test_a_stalled_watcher_loses_steps_rather_than_stalling_the_ingest(self) -> None:
        bus = ProgressBus(backlog=2)
        with bus.subscribe() as queue:
            for _ in range(10):
                bus.publish(_p(Stage.READING))  # must not raise or block
            assert queue.qsize() == 2

    def test_the_event_carries_the_rendered_label(self) -> None:
        payload = json.loads(as_event(_p(Stage.FILING)).removeprefix("data: ").strip())
        assert payload["label"] == "옮기고 사이드카 쓰는 중"
        assert payload["stage"] == "filing"
        assert payload["fraction"] is None


class TestCardProgress:
    async def test_one_reading_step_per_window(self, llm) -> None:  # type: ignore[no-untyped-def]
        seen: list[Progress] = []
        text = "가나다라마바사아자차카타파하" * 40
        extraction = Extraction(sections=(Section(text=text, order=0),), parser="test")

        await CardService(llm, context_chars=100).describe(
            extraction, filename="긴문서.pdf", on_progress=seen.append
        )

        reading = [p for p in seen if p.stage is Stage.READING]
        windows = len(extraction.windows(100))
        assert {p.step for p in reading} == set(range(1, windows + 1))
        assert all(p.steps == windows for p in reading)
        assert any(p.stage is Stage.DENSIFYING for p in seen)

    async def test_a_window_reports_what_it_actually_found(self, llm) -> None:  # type: ignore[no-untyped-def]
        seen: list[Progress] = []
        extraction = Extraction(
            sections=(Section(text="가나다라마바사아자차카타파하" * 40, order=0),), parser="test"
        )
        await CardService(llm, context_chars=100).describe(
            extraction, filename="긴문서.pdf", on_progress=seen.append
        )
        # The scripted update offers 지연배상 / 대한물산; only the first window can be new.
        found = [f for p in seen for f in p.found]
        assert found.count("지연배상") == 1
        assert found.count("대한물산") == 1


class TestIngestProgress:
    async def test_the_steps_of_one_document_in_order(
        self, engine: Bismuth, make_document: Callable[..., Path]
    ) -> None:
        seen: list[Progress] = []
        source = make_document("계약.txt", "아폴로 지원 계약서, 2023.")
        rel = engine.ingest.stage(source.read_bytes(), source.name)

        await engine.ingest.process(rel, on_progress=seen.append)

        stages = [p.stage for p in seen]
        for stage in (
            Stage.RECEIVED,
            Stage.PARSING,
            Stage.PARSED,
            Stage.READING,
            Stage.CARDED,
            Stage.PLACING,
            Stage.PLACED,
            Stage.FILING,
            Stage.DONE,
        ):
            assert stage in stages, f"{stage} never reported"
        assert (
            stages.index(Stage.PARSING) < stages.index(Stage.READING) < stages.index(Stage.PLACING)
        )
        assert stages[-1] is Stage.DONE
        assert all(p.filename == "계약.txt" for p in seen)

    async def test_the_parse_step_names_the_parser_and_the_extent(
        self, engine: Bismuth, make_document: Callable[..., Path]
    ) -> None:
        seen: list[Progress] = []
        source = make_document("계약.txt", "아폴로 지원 계약서, 2023.")
        await engine.ingest.process(
            engine.ingest.stage(source.read_bytes(), source.name), on_progress=seen.append
        )
        parsing = next(p for p in seen if p.stage is Stage.PARSING)
        parsed = next(p for p in seen if p.stage is Stage.PARSED)
        assert parsing.note == "plain"
        assert "자" in parsed.note

    async def test_the_landing_step_never_carries_the_whole_rationale(
        self, engine: Bismuth, script: ScriptedModel, make_document: Callable[..., Path]
    ) -> None:
        """The rationale is a paragraph; a progress line that long buries every other step."""
        from tests.conftest import seed_folder

        seed_folder(Path(engine.vault.root), PurePosixPath("환경/생태계"))
        script.set(
            placement_prompts.PlacementDecision,
            placement_prompts.PlacementDecision(
                folder="환경/생태계",
                existing=False,
                confidence=0.12,
                reason="제시된 폴더들은 미생물학 자료에 한정되어 있고 " * 8,
            ),
        )
        seen: list[Progress] = []
        source = make_document("논문.txt", "생태계서비스 논문")
        await engine.ingest.process(
            engine.ingest.stage(source.read_bytes(), source.name), on_progress=seen.append
        )

        placed = next(p for p in seen if p.stage is Stage.PLACED)
        # Low confidence no longer parks anything; it is filed and the number is recorded.
        assert placed.note == "환경/생태계"
        assert len(placed.note) < 60

    async def test_the_root_reads_as_a_destination_not_a_failure(
        self, engine: Bismuth, script: ScriptedModel, make_document: Callable[..., Path]
    ) -> None:
        script.set(
            placement_prompts.PlacementDecision,
            placement_prompts.PlacementDecision(
                folder="", existing=False, confidence=0.9, reason="아직 나눌 구분이 없습니다."
            ),
        )
        seen: list[Progress] = []
        source = make_document("첫문서.txt", "무언가")
        await engine.ingest.process(
            engine.ingest.stage(source.read_bytes(), source.name), on_progress=seen.append
        )

        assert next(p for p in seen if p.stage is Stage.PLACED).note.startswith("루트")

    async def test_the_final_step_says_where_it_landed(
        self, engine: Bismuth, make_document: Callable[..., Path]
    ) -> None:
        seen: list[Progress] = []
        source = make_document("계약.txt", "아폴로 지원 계약서, 2023.")
        result = await engine.ingest.process(
            engine.ingest.stage(source.read_bytes(), source.name), on_progress=seen.append
        )
        assert seen[-1].note == str(result.destination)

    async def test_a_duplicate_stops_early_and_says_where_the_original_is(
        self, engine: Bismuth, make_document: Callable[..., Path]
    ) -> None:
        source = make_document("계약.txt", "아폴로 지원 계약서, 2023.")
        await engine.ingest.process(engine.ingest.stage(source.read_bytes(), source.name))

        seen: list[Progress] = []
        again = engine.ingest.stage(source.read_bytes(), "다른이름.txt")
        await engine.ingest.process(again, on_progress=seen.append)

        assert [p.stage for p in seen] == [Stage.RECEIVED, Stage.DUPLICATE]
        assert seen[-1].note == "아폴로/2023"

    async def test_ingest_survives_a_listener_that_throws(
        self, engine: Bismuth, make_document: Callable[..., Path]
    ) -> None:
        def explode(_: Progress) -> None:
            raise RuntimeError("the UI is on fire")

        source = make_document("계약.txt", "아폴로 지원 계약서, 2023.")
        result = await engine.ingest.process(
            engine.ingest.stage(source.read_bytes(), source.name), on_progress=explode
        )
        assert result.placement.is_placed


class TestProgressEndpoint:
    async def test_the_stream_opens_then_carries_each_step(self) -> None:
        """Driven directly: the stream never ends by design, so a client that reads it to
        completion (TestClient) hangs forever."""
        bus = ProgressBus()
        events = stream(bus)
        try:
            assert await anext(events) == ": open\n\n"
            await asyncio.sleep(0)  # let the generator reach its first await, i.e. subscribe
            bus.publish(Progress(stage=Stage.PLACED, filename="계약.pdf", note="아폴로/2023"))
            chunk = await anext(events)
            assert chunk.startswith("data: ") and chunk.endswith("\n\n")
            assert json.loads(chunk.removeprefix("data: "))["note"] == "아폴로/2023"
        finally:
            await events.aclose()
        assert bus.watchers == 0  # closing the tab unsubscribes

    async def test_the_endpoint_answers_as_an_unbuffered_event_stream(
        self, client: TestClient
    ) -> None:
        route = next(r for r in client.app.routes if getattr(r, "path", "") == "/api/progress")  # type: ignore[attr-defined]
        response = await route.endpoint()  # type: ignore[attr-defined]
        try:
            assert response.media_type == "text/event-stream"
            assert response.headers["cache-control"] == "no-cache"
            # Without this a reverse proxy holds the whole stream until it ends -- i.e. forever.
            assert response.headers["x-accel-buffering"] == "no"
        finally:
            await response.body_iterator.aclose()

    def test_uploading_publishes_every_step_to_the_bus(self, client: TestClient) -> None:
        """The endpoint is wired to the pipeline, not just to a stream that stays empty."""
        seen: list[Progress] = []
        bus: ProgressBus = client.app.state.progress  # type: ignore[attr-defined]
        bus.publish = seen.append  # type: ignore[method-assign]

        client.post(
            "/api/documents", files={"files": ("계약.txt", "아폴로 계약서".encode(), "text/plain")}
        )

        stages = [p.stage for p in seen]
        assert Stage.RECEIVED in stages and Stage.READING in stages
        assert stages[-1] is Stage.DONE

    def test_a_scan_reports_too(self, client: TestClient, vault_path: Path) -> None:
        """Progress is per-document, not per-upload, so hand-dropped files show the same steps."""
        inbox = vault_path / "_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "손으로넣은.txt").write_text("아폴로 계약서", encoding="utf-8")

        seen: list[Progress] = []
        bus: ProgressBus = client.app.state.progress  # type: ignore[attr-defined]
        bus.publish = seen.append  # type: ignore[method-assign]

        client.post("/api/scan")
        assert [p.filename for p in seen] == ["손으로넣은.txt"] * len(seen)
        assert seen[-1].stage is Stage.DONE
