"""The two-question pipeline: file a batch, look at the tree when it has grown."""

from __future__ import annotations

from pathlib import PurePosixPath

from bismuth.container import Bismuth
from bismuth.prompts import simple as simple_prompts
from bismuth.services import simple as simple_service
from tests.conftest import ScriptedModel


class Answers:
    """A model that replies with whatever the test lined up, in order."""

    def __init__(self, script: ScriptedModel) -> None:
        self.script = script
        self.replies: list[str] = []
        self.asked: list[str] = []

    def __call__(self, prompt, schema):  # type: ignore[no-untyped-def]
        if "DOCUMENTS TO FILE" in prompt.user or "THE TREE:" in prompt.user:
            self.asked.append(prompt.user)
            return self.replies.pop(0) if self.replies else "KEEP"
        return self.script(prompt, schema)


async def _staged(engine: Bismuth, count: int) -> list:
    """Documents read and catalogued, sitting in the inbox, not yet filed."""
    out = []
    for index in range(count):
        rel = engine.ingest.stage(f"문서 {index} 내용".encode(), f"doc{index}.txt")
        out.append(await engine.ingest.prepare(rel))
    return out


def _batch(prepared: list) -> list:
    return [(one.rel, one.card, one) for one in prepared]


class TestFilingAHandfulAtOnce:
    async def test_one_call_files_the_whole_batch(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm._handler = answers  # type: ignore[attr-defined]
        prepared = await _staged(engine, 3)
        answers.replies = ["D1: 문학\nD2: 문학\nD3: ROOT\nSIGN: 문학 | 소설과 시를 모아둔다"]

        await engine.simple.file(_batch(prepared))

        assert len(answers.asked) == 1, "one call for the batch, not one per document"
        assert (engine.vault.root / "문학/doc0.txt").is_file()
        assert (engine.vault.root / "문학/doc1.txt").is_file()
        assert (engine.vault.root / "doc2.txt").is_file(), "ROOT means the pile, honestly"
        assert (engine.vault.root / "문학/_folder.md").is_file()

    async def test_a_named_folder_carries_the_sign_it_was_given(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm._handler = answers  # type: ignore[attr-defined]
        prepared = await _staged(engine, 2)
        answers.replies = ["D1: 문학\nD2: 문학\nSIGN: 문학 | 소설과 시를 모아둔다"]

        await engine.simple.file(_batch(prepared))

        note = engine.charters.load(PurePosixPath("문학"))
        assert note is not None
        assert note.purpose == "소설과 시를 모아둔다"

    async def test_a_nested_path_makes_every_level(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm._handler = answers  # type: ignore[attr-defined]
        prepared = await _staged(engine, 2)
        answers.replies = ["D1: 인문/문학\nD2: 인문/문학"]

        await engine.simple.file(_batch(prepared))

        assert (engine.vault.root / "인문/문학/doc0.txt").is_file()
        assert (engine.vault.root / "인문/_folder.md").is_file()
        assert (engine.vault.root / "인문/문학/_folder.md").is_file()

    async def test_the_tree_it_is_shown_is_the_tree_that_stands(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm._handler = answers  # type: ignore[attr-defined]
        first = await _staged(engine, 2)
        answers.replies = ["D1: 문학\nD2: 문학\nSIGN: 문학 | 소설과 시"]
        await engine.simple.file(_batch(first))
        second = await _staged(engine, 1)
        answers.replies = ["D1: 문학"]

        await engine.simple.file(_batch(second))

        shown = answers.asked[-1]
        assert "문학/  (2 here) — 소설과 시" in shown


class TestLookingAtTheWholeTree:
    async def test_it_waits_until_the_collection_is_worth_judging(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm._handler = answers  # type: ignore[attr-defined]
        prepared = await _staged(engine, 3)
        answers.replies = ["D1: 문학\nD2: 문학\nD3: 문학"]
        await engine.simple.file(_batch(prepared))

        assert not engine.simple.due(), "three documents is not a tree to judge"

    async def test_keep_moves_nothing(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm._handler = answers  # type: ignore[attr-defined]
        prepared = await _staged(engine, 2)
        answers.replies = ["D1: 문학\nD2: 문학", "KEEP"]
        await engine.simple.file(_batch(prepared))

        assert engine.simple.due()
        assert not await engine.simple.review()
        assert (engine.vault.root / "문학/doc0.txt").is_file()

    async def test_a_move_relocates_the_folder_whole(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm._handler = answers  # type: ignore[attr-defined]
        prepared = await _staged(engine, 2)
        answers.replies = [
            "D1: 문학\nD2: 문학\nSIGN: 문학 | 소설과 시",
            "MOVE: 문학 | 인문/문학\nSIGN: 인문 | 사람이 쓴 것",
        ]
        await engine.simple.file(_batch(prepared))

        assert await engine.simple.review()

        assert (engine.vault.root / "인문/문학/doc0.txt").is_file()
        assert (engine.vault.root / "인문/문학/doc1.txt").is_file()
        assert not (engine.vault.root / "문학").exists()
        note = engine.charters.load(PurePosixPath("인문"))
        assert note is not None and note.purpose == "사람이 쓴 것"

    async def test_it_is_not_asked_again_until_the_collection_doubles(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm._handler = answers  # type: ignore[attr-defined]
        prepared = await _staged(engine, 2)
        answers.replies = ["D1: 문학\nD2: 문학", "KEEP"]
        await engine.simple.file(_batch(prepared))
        await engine.simple.review()

        assert not engine.simple.due(), "two documents were just judged"

        more = await _staged(engine, 2)
        answers.replies = ["D1: 문학\nD2: 문학"]
        await engine.simple.file(_batch(more))

        assert engine.simple.due(), "four is twice two"

    async def test_a_folder_cannot_be_moved_inside_itself(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm._handler = answers  # type: ignore[attr-defined]
        prepared = await _staged(engine, 2)
        answers.replies = ["D1: 문학\nD2: 문학", "MOVE: 문학 | 문학/하위"]
        await engine.simple.file(_batch(prepared))

        assert not await engine.simple.review()
        assert (engine.vault.root / "문학/doc0.txt").is_file()


class TestWhatTheReplyMaySay:
    def test_a_document_with_no_line_stays_at_the_root(self) -> None:
        placed, _ = simple_prompts.parse_filing("D1: 문학")

        assert placed == {"D1": "문학"}

    def test_a_line_nobody_asked_for_is_dropped(self) -> None:
        placed, signs = simple_prompts.parse_filing("여기 있습니다:\nD1: 문학\n감사합니다.")

        assert placed == {"D1": "문학"}
        assert not signs

    def test_keep_and_an_empty_reply_mean_the_same(self) -> None:
        assert simple_prompts.parse_review("KEEP")[0]
        assert simple_prompts.parse_review("")[0]
        assert simple_prompts.parse_review("이 트리는 괜찮습니다.")[0]
