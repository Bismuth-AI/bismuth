"""The whole-collection pass: the only operation not asked from inside a folder."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from bismuth.container import Bismuth
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import redesign as redesign_prompts
from tests.conftest import ScriptedModel
from tests.test_ingest import add, place_at


def _designed(script: ScriptedModel, *names: str, axis: str = "규제 대상 분야") -> None:
    """Script the one call that decides the top of the tree."""
    script.set(
        redesign_prompts.Design,
        redesign_prompts.Design(
            question=f"이 문서의 {axis}는 무엇인가?",
            axis=axis,
            classes=[
                redesign_prompts.Class(name=name, sign=f"{name}에 관한 문서") for name in names
            ],
        ),
    )


async def _three_folders(engine: Bismuth, script: ScriptedModel) -> None:
    """A root holding three folders, each with a document in it."""
    from tests.conftest import seed_folder

    for name in ("문학", "역사", "과학"):
        seed_folder(Path(engine.vault.root), PurePosixPath(name))
        script.set(placement_prompts.PlacementDecision, place_at(name))
        await add(engine, f"{name}.txt", f"{name} 문서 내용")


def _drawn_at(engine: Bismuth, documents: int) -> None:
    """Say the top of this tree was last drawn when it held ``documents``."""
    from bismuth.domain.charter import Charter

    note = Charter(
        path=PurePosixPath(),
        title="/",
        purpose="",
        split_basis="다루는 분야",
        split_question="이 문서의 다루는 분야는 무엇인가?",
        split_at_documents=documents,
    )
    (Path(engine.vault.root) / "_folder.md").write_text(note.to_markdown(), encoding="utf-8")


class TestDrawingANewTopLevel:
    async def test_folders_move_whole_under_the_new_names(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """No document changes the folder it is in; only the path above it does."""
        await _three_folders(engine, script)
        _designed(script, "인문", "자연", "사회")
        script.set_assigned({"문학": "C001", "역사": "C001", "과학": "C002"})

        result = await engine.redesign.redesign()

        assert result.applied
        assert (engine.vault.root / "인문/문학/문학.txt").is_file()
        assert (engine.vault.root / "인문/역사").is_dir()
        assert (engine.vault.root / "자연/과학").is_dir()
        assert not (engine.vault.root / "문학").exists()
        # The folder note travelled with the folder it describes.
        assert (engine.vault.root / "인문/문학/_folder.md").is_file()

    async def test_the_root_records_the_question_it_was_redrawn_on(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _three_folders(engine, script)
        _designed(script, "인문", "자연", "사회", axis="다루는 분야")
        script.set_assigned({"문학": "C001", "역사": "C001", "과학": "C002"})

        await engine.redesign.redesign()

        note = engine.charters.load(PurePosixPath())
        assert note is not None
        assert note.split_basis == "다루는 분야"
        assert note.split_question == "이 문서의 다루는 분야는 무엇인가?"

    async def test_a_folder_nothing_claims_stays_where_it_is(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """STAY is the safe answer: a wrong move puts a whole shelf out of reach."""
        await _three_folders(engine, script)
        _designed(script, "인문", "자연", "사회")
        script.set_assigned({"문학": "C001", "역사": "C001"})

        result = await engine.redesign.redesign()

        assert result.applied
        assert (engine.vault.root / "과학/과학.txt").is_file()
        assert "과학" not in result.moved_folders

    async def test_it_is_one_undoable_batch(self, engine: Bismuth, script: ScriptedModel) -> None:
        """A redesign that stops half way leaves one collection in two schemes."""
        await _three_folders(engine, script)
        _designed(script, "인문", "자연", "사회")
        script.set_assigned({"문학": "C001", "역사": "C001", "과학": "C002"})
        await engine.redesign.redesign()

        entry = next(e for e in engine.journal.iter_entries() if "redesign" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "문학/문학.txt").is_file()
        assert not (engine.vault.root / "인문").exists()


class TestWhatItRefusesBeforeMovingAnything:
    async def test_a_design_that_renames_the_folders_that_already_stand_here(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """Nothing has been redrawn, and finding that out must not cost the loop."""
        await _three_folders(engine, script)
        _designed(script, "문학", "역사", "과학")
        llm.calls.clear()

        result = await engine.redesign.redesign()

        assert not result.applied
        assert "already stand here" in result.refused
        assert not [p for p in llm.prompts_for(None) if "THE NEW TOP-LEVEL FOLDERS:" in p.user]

    async def test_fewer_than_three_answers_is_a_rename(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _three_folders(engine, script)
        _designed(script, "인문", "자연")

        result = await engine.redesign.redesign()

        assert not result.applied
        assert not (engine.vault.root / "인문").exists()

    async def test_a_name_that_repeats_the_property_is_refused(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The same contract every other name in this program is held to."""
        await _three_folders(engine, script)
        _designed(script, "규제 대상 분야", "자연", "사회")

        result = await engine.redesign.redesign()

        assert not result.applied
        assert not (engine.vault.root / "자연").exists()

    async def test_an_empty_vault_is_not_redrawn(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        result = await engine.redesign.redesign()

        assert not result.applied
        assert "not enough standing here" in result.refused


class TestTheCostIsBoundedByFolders:
    async def test_a_folder_of_four_hundred_costs_the_same_question_as_a_folder_of_two(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """This is what keeps the pass O(folders) rather than O(documents)."""
        from tests.conftest import seed_folder

        for name in ("문학", "역사", "과학"):
            seed_folder(Path(engine.vault.root), PurePosixPath(name))
            script.set(placement_prompts.PlacementDecision, place_at(name))
            for index in range(4 if name == "문학" else 1):
                await add(engine, f"{name}{index}.txt", f"{name} 문서 {index}")
        # One document nothing has filed yet: those are the only ones asked about
        # individually, because no folder can speak for them.
        script.set(placement_prompts.PlacementDecision, place_at(""))
        await add(engine, "loose.txt", "아직 어디에도 안 들어간 문서")
        _designed(script, "인문", "자연", "사회")
        script.set_assigned({"문학": "C001", "역사": "C001", "과학": "C002"})
        standing = [
            item
            for item in Path(engine.vault.root).iterdir()
            if item.is_dir() and not item.name.startswith(("_", "."))
        ]
        llm.calls.clear()

        await engine.redesign.redesign()

        asked = [p for p in llm.prompts_for(None) if "THE NEW TOP-LEVEL FOLDERS:" in p.user]
        assert len(asked) == len(standing) + 1, (
            "one question per folder, plus one for the single loose document"
        )
        # The folder holding four documents was asked exactly once, like every other.
        deep = [p for p in asked if "WHAT IS BEING PLACED: 문학" in p.user]
        assert len(deep) == 1


class TestAClassThatOnlyWrapsAFolder:
    """SPEC 6.2 counts pass-through folders and wants none. The first real redesign made
    three: 금융 및 금융소비자 was drawn over 금융업 및 금융소비자 and held 89 of its 90
    documents in that one child."""

    async def test_a_name_that_says_a_standing_folder_again_is_refused(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _three_folders(engine, script)
        _designed(script, "문학 및 예술", "자연", "사회")

        result = await engine.redesign.redesign()

        assert not result.applied
        assert "문학" in result.refused
        assert not (engine.vault.root / "자연").exists()

    async def test_names_that_share_no_words_with_what_stands_here_are_fine(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _three_folders(engine, script)
        _designed(script, "인문", "자연", "사회")
        script.set_assigned({"문학": "C001", "역사": "C001", "과학": "C002"})

        result = await engine.redesign.redesign()

        assert result.applied


class TestThePropertyIsCheckedHereToo:
    """The pass drew 행정부처 관할 -- who administers the document -- which every division
    inside a folder refuses. The names were subjects; the question was not, and the
    question governs every later division of the root."""

    async def test_a_refused_property_moves_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _three_folders(engine, script)
        _designed(script, "인문", "자연", "사회")
        script.set_assigned({"문학": "C001", "역사": "C001", "과학": "C002"})
        script.set_axis_fails()

        result = await engine.redesign.redesign()

        assert not result.applied
        assert not (engine.vault.root / "인문").exists()

    async def test_the_second_ask_is_told_what_was_turned_down(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """Refusing outright would leave the collection undrawn until it doubles again."""
        await _three_folders(engine, script)
        _designed(script, "인문", "자연", "사회", axis="행정부처 관할")
        script.set_axis_fails()

        await engine.redesign.redesign()

        asked = llm.prompts_for(redesign_prompts.Design)
        assert len(asked) == 2, "asked twice at most, and the second knows why"
        assert "행정부처 관할" in asked[-1].user
        assert "TURNED DOWN" in asked[-1].user


class TestWhenItRunsItself:
    """The product is that a person uploads documents and nothing else (SPEC 5), so a
    correction pass with a button and no schedule is not one."""

    async def test_a_collection_nobody_has_drawn_yet_is_left_alone(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The incremental path decides when a root first has enough to divide at all."""
        await _three_folders(engine, script)

        assert not engine.redesign.due()

    async def test_it_waits_for_the_collection_to_double(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _three_folders(engine, script)
        here = engine.vault.count_files(PurePosixPath(), recursive=True)

        _drawn_at(engine, here)
        assert not engine.redesign.due(), "a top level drawn over this many is not stale"

        _drawn_at(engine, max(1, here // 2))
        assert engine.redesign.due(), "the collection has doubled since it was drawn"

    async def test_an_arrival_that_crosses_it_redraws_the_top(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Nobody pressed anything: a document arrived and the tree was redrawn."""
        await _three_folders(engine, script)
        _drawn_at(engine, 1)
        _designed(script, "인문", "자연", "사회")
        script.set_assigned({"문학": "C001", "역사": "C001", "과학": "C002"})
        script.set(placement_prompts.PlacementDecision, place_at(""))

        await add(engine, "새문서.txt", "새로 올린 문서")

        assert (engine.vault.root / "인문/문학").is_dir()
