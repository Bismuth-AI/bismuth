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
    """Say this pass last drew the top of the tree when it held ``documents``."""
    from bismuth.domain.charter import Charter

    note = Charter(
        path=PurePosixPath(),
        title="/",
        purpose="",
        split_basis="다루는 분야",
        split_question="이 문서의 다루는 분야는 무엇인가?",
        split_at_documents=documents,
        redrawn_at_documents=documents,
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
    async def test_a_design_that_leaves_the_tree_alone_is_an_answer(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """The answer every other question here can give and this one could not. Without
        it the model answered with the folders already standing -- the only true answer
        left to it -- and was turned down for that 26 times in one run."""
        await _three_folders(engine, script)
        script.set(redesign_prompts.Design, redesign_prompts.Design(question="?", axis="주제"))
        llm.calls.clear()

        result = await engine.redesign.redesign()

        assert not result.applied
        assert not [p for p in llm.prompts_for(None) if "THE NEW TOP-LEVEL FOLDERS:" in p.user]
        assert (engine.vault.root / "문학").is_dir()

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
            for item in Path(engine.vault.root).rglob("*")
            if item.is_dir()
            and not item.name.startswith(("_", "."))
            and ".bismuth" not in item.parts
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
    documents in that one child.

    Whether a class is one is a fact about what it collected, not about its name. Judged
    by name, 연구개발 및 과학기술 was turned down 55 times for standing over 연구개발 --
    which is the broader shelf the pass exists to build."""

    async def test_a_class_that_collects_one_folder_it_repeats_is_dropped(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _three_folders(engine, script)
        _designed(script, "문학 및 예술", "자연", "사회")
        script.set_assigned({"문학": "C001", "역사": "C002", "과학": "C002"})

        result = await engine.redesign.redesign()

        assert not (engine.vault.root / "문학 및 예술").exists(), "it would hold 문학 alone"
        assert (engine.vault.root / "문학/문학.txt").is_file()
        # The rest of the redesign still happened.
        assert result.applied
        assert (engine.vault.root / "자연/역사").is_dir()

    async def test_a_broader_name_that_collects_several_is_kept(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """연구개발 및 과학기술 over 연구개발 and one more is the whole point."""
        await _three_folders(engine, script)
        _designed(script, "문학 및 예술", "자연", "사회")
        script.set_assigned({"문학": "C001", "역사": "C001", "과학": "C002"})

        result = await engine.redesign.redesign()

        assert result.applied
        assert (engine.vault.root / "문학 및 예술/문학").is_dir()
        assert (engine.vault.root / "문학 및 예술/역사").is_dir()

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
        # Filing those three already asked the schedule, and it answered yes: three
        # folders stand here and this pass has never drawn any of them.
        llm.calls.clear()

        await engine.redesign.redesign()

        asked = llm.prompts_for(redesign_prompts.Design)
        assert len(asked) == 2, "asked twice at most, and the second knows why"
        assert "행정부처 관할" in asked[-1].user
        assert "TURNED DOWN" in asked[-1].user


class TestWhenItRunsItself:
    """The product is that a person uploads documents and nothing else (SPEC 5), so a
    correction pass with a button and no schedule is not one."""

    async def test_a_root_with_nothing_standing_on_it_is_left_alone(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The incremental path decides when a root first has enough to divide at all."""
        assert not engine.redesign.due()

    async def test_the_first_look_waits_for_a_top_level_to_exist(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Until this pass has looked there is no count of its own to double, and the one
        it would have to borrow is the one that keeps moving."""
        from tests.conftest import seed_folder

        for name in ("문학", "역사", "과학"):
            seed_folder(Path(engine.vault.root), PurePosixPath(name))

        assert engine.redesign.due(), "three folders stand here and it has never looked"

    async def test_a_look_that_moved_nothing_still_counts_as_a_look(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """160 attempts in one run, 155 of which changed anything -- because only an
        applied redesign wrote anything down."""
        await _three_folders(engine, script)
        script.set(redesign_prompts.Design, redesign_prompts.Design(question="?", axis="주제"))

        await engine.redesign.redesign()

        assert not engine.redesign.due()

    async def test_the_clock_the_incremental_path_keeps_resetting_is_not_used(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Replayed against a real run, measuring against split_at_documents answered no
        on all three hundred arrivals: the root divided eighteen times and reset it."""
        from bismuth.domain.charter import Charter

        await _three_folders(engine, script)
        engine.redesign._looked_at = 0  # type: ignore[attr-defined]  # this test is about the note
        here = max(1, engine.vault.count_files(PurePosixPath(), recursive=True))
        note = Charter(
            path=PurePosixPath(),
            title="/",
            purpose="",
            split_basis="다루는 분야",
            split_question="이 문서의 다루는 분야는 무엇인가?",
            split_at_documents=here * 100,
            redrawn_at_documents=max(1, here // 2),
        )
        (Path(engine.vault.root) / "_folder.md").write_text(note.to_markdown(), encoding="utf-8")

        assert engine.redesign.due(), "our own record has doubled, whatever theirs says"

    async def test_after_the_first_it_waits_for_the_collection_to_double(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _three_folders(engine, script)
        engine.redesign._looked_at = 0  # type: ignore[attr-defined]  # this test is about the note
        here = max(1, engine.vault.count_files(PurePosixPath(), recursive=True))

        _drawn_at(engine, here)
        assert not engine.redesign.due(), "a top level drawn over this many is not stale"

        _drawn_at(engine, max(1, here // 2))
        assert engine.redesign.due(), "the collection has doubled since it was drawn"

    async def test_drawing_it_records_when(self, engine: Bismuth, script: ScriptedModel) -> None:
        """Otherwise it would be due again on the very next arrival, for ever."""
        await _three_folders(engine, script)
        _designed(script, "인문", "자연", "사회")
        script.set_assigned({"문학": "C001", "역사": "C001", "과학": "C002"})

        await engine.redesign.redesign()

        note = engine.charters.load(PurePosixPath())
        assert note is not None
        assert note.redrawn_at_documents == engine.vault.count_files(
            PurePosixPath(), recursive=True
        )
        assert not engine.redesign.due()

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
