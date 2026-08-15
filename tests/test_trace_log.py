"""The trace is written for whoever has to work out what the pipeline did.

Read as a debugging record, the two files have to answer three things: which document
a line belongs to, when it happened, and -- when nothing happened -- why not.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath

import pytest

from bismuth.container import Bismuth
from bismuth.domain.charter import Charter
from bismuth.logging_setup import configure_logging, log_llm_call, log_trace
from bismuth.ports.llm import CURRENT_DOCUMENT
from bismuth.prompts import placement as placement_prompts
from tests.conftest import ScriptedModel
from tests.test_ingest import add, place_at


@pytest.fixture
def logs(tmp_path: Path):
    """Logging pointed somewhere disposable, and put back afterwards."""
    directory = tmp_path / "logs"
    configure_logging(log_dir=directory)
    yield directory
    for name in ("bismuth", "bismuth.llm", "bismuth.trace"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestJoinable:
    def test_a_model_call_names_the_document_it_was_made_for(self, logs: Path) -> None:
        """Line order used to answer this. Documents are now read several at a time,
        so the file has to say it."""
        CURRENT_DOCUMENT.set("doc-42")
        log_llm_call({"call": "#1", "schema": "CardDraft"})

        record = _lines(logs / "llm.jsonl")[0]
        assert record["document_id"] == "doc-42"
        assert record["call"] == "#1"

    def test_every_line_is_stamped(self, logs: Path) -> None:
        CURRENT_DOCUMENT.set("doc-42")
        log_trace("card.begin", document_id="doc-42")
        log_llm_call({"call": "#1"})

        assert _lines(logs / "trace.jsonl")[0]["t"]
        assert _lines(logs / "llm.jsonl")[0]["t"]

    async def test_the_two_files_can_be_joined_on_a_document(
        self, engine: Bismuth, logs: Path
    ) -> None:
        await add(engine, "contract.txt")

        traced = {line["document_id"] for line in _lines(logs / "trace.jsonl")}
        assert len(traced) == 1 and "" not in traced


class TestStages:
    async def test_a_decision_carries_the_stage_that_produced_it(
        self, engine: Bismuth, logs: Path
    ) -> None:
        """A context that covers only the model call leaves the decision untagged, so
        filtering by stage returns the calls and not what they decided."""
        await add(engine, "contract.txt")

        staged = {
            line["event"]: line.get("stage")
            for line in _lines(logs / "trace.jsonl")
            if line.get("stage")
        }

        assert staged["place.decided"] == "placement"
        assert staged["subdivide.emerging"] == "subdivision.emerging"
        assert staged["card.window"].startswith("card.")

    async def test_each_card_window_is_its_own_window_id(self, engine: Bismuth, logs: Path) -> None:
        await add(engine, "contract.txt")

        windows = {
            line["window_id"] for line in _lines(logs / "trace.jsonl") if line.get("window_id")
        }

        assert any(window.endswith(":window-001") for window in windows)


class TestPlacement:
    async def test_where_a_document_went_is_in_the_trace(self, engine: Bismuth, logs: Path) -> None:
        """It was only ever in llm.jsonl, as a prompt and a reply to read by hand --
        and that is the decision people ask about most."""
        await add(engine, "contract.txt")

        decided = [
            line for line in _lines(logs / "trace.jsonl") if line["event"] == "place.decided"
        ]

        assert len(decided) == 1
        assert decided[0]["chose"] == "아폴로/2023"
        assert decided[0]["created_folder"] is False
        assert decided[0]["steps"]
        assert "reason" not in decided[0]

    async def test_the_root_is_recorded_as_the_root(
        self, engine: Bismuth, script: ScriptedModel, logs: Path
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at(""))

        await add(engine, "unsorted.txt")

        decided = next(
            line for line in _lines(logs / "trace.jsonl") if line["event"] == "place.decided"
        )
        assert decided["root"] is True
        assert decided["created_folder"] is False


class TestSilence:
    async def test_every_arrival_asks(
        self, engine: Bismuth, script: ScriptedModel, logs: Path
    ) -> None:
        """A power-of-two schedule asked the root of a thirty-document archive four
        times, all of them early, and never again after the sixteenth document. Nothing
        was ever filed. Arrival is what makes the question worth asking."""
        script.set(placement_prompts.PlacementDecision, place_at(""))
        for index in range(3):
            await add(engine, f"doc{index}.txt", f"문서 {index}")

        asked = [
            line for line in _lines(logs / "trace.jsonl") if line["event"] == "subdivide.emerging"
        ]

        assert [line["documents"] for line in asked] == [1, 2, 3]

    async def test_a_folder_left_alone_says_why(
        self, engine: Bismuth, script: ScriptedModel, logs: Path
    ) -> None:
        """Not asked and asked-then-declined must not look identical."""
        script.set(placement_prompts.PlacementDecision, place_at(""))
        await add(engine, "one.txt", "문서 하나")
        note = Charter(path=PurePosixPath(), title="내 서재", purpose="직접 정리함", managed=False)
        (engine.vault.root / "_folder.md").write_text(note.to_markdown(), encoding="utf-8")

        await engine.subdivision.consider(PurePosixPath())

        skipped = [
            line for line in _lines(logs / "trace.jsonl") if line["event"] == "subdivide.skipped"
        ]
        assert skipped[-1]["reason"] == "folder note is not managed"
