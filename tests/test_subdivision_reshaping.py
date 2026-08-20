"""MERGE and SPLIT: standing folders together, and dissolving a level."""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from bismuth.container import Bismuth
from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.maintenance import validate_grouping
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import subdivision as subdivision_prompts
from tests.conftest import ScriptedModel
from tests.subdivision_helpers import _emerges, _fill
from tests.test_ingest import add, place_at


class TestStandingFoldersTogether:
    """The fourth operation: a level that grew too wide can be narrowed again."""

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        """Handles are request-local, so the two documents still loose are always D0001-2."""
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"], once=True)
            await engine.subdivision.consider(PurePosixPath())

    async def test_several_folders_move_under_one_broader_name(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="인문", sign="문학과 역사"),
        )
        script.set_shelved(["문학", "역사"])

        await self._three_shelves(engine, script)

        assert (engine.vault.root / "인문/문학").is_dir()
        assert (engine.vault.root / "인문/역사").is_dir()
        # No document changed the folder it is in; only the path above it changed.
        assert (engine.vault.root / "인문/문학/doc0.txt").is_file()
        assert not (engine.vault.root / "문학").exists()
        # And something stayed behind, or this was a rename.
        assert (engine.vault.root / "과학").is_dir()

    async def test_a_shelf_that_would_take_every_folder_is_refused(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="자료", sign="모든 자료"),
        )
        # 아폴로 is the seeded fixture folder, so this really is every folder here.
        script.set_shelved(["문학", "과학", "역사", "아폴로"])

        await self._three_shelves(engine, script)

        assert not (engine.vault.root / "자료").exists()
        assert (engine.vault.root / "문학").is_dir()

    async def test_standing_folders_together_is_one_undoable_batch(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="인문", sign="문학과 역사"),
        )
        script.set_shelved(["문학", "역사"])
        await self._three_shelves(engine, script)

        entry = next(e for e in engine.journal.iter_entries() if "group" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "문학/doc0.txt").is_file()
        assert not (engine.vault.root / "인문").exists()


class TestNamingAShelfThatAlreadyStands:
    async def test_the_loose_documents_go_behind_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Not a mistake: an answer to a different question, and it used to be thrown
        away -- 119 times at one root, which is why 114 documents stayed loose."""
        await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ["D0001", "D0002"])
        await engine.subdivision.consider(PurePosixPath())
        assert (engine.vault.root / "문학/doc0.txt").is_file()

        # The same name again, over documents that are still loose.
        _emerges(script, "문학", "문학 자료", ["D0001", "D0002"])

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "문학/doc2.txt").is_file()
        assert not (engine.vault.root / "doc2.txt").exists()


class TestALevelThatDoesNotEarnItsGuess:
    """The fourth operator (ADR-0018), and the one this library did not have.

    Without it a level, once drawn, is permanent. One branch reached seven levels, six of
    whose seven segments contained 금융, and every one had been locally justified when it
    was drawn: nothing could ever look at the path and shorten it. Cobweb calls merge and
    split reverse operators whose purpose is to correct mistakes made on earlier turns.
    """

    async def _two_levels(self, engine: Bismuth, script: ScriptedModel) -> list[str]:
        shutil.rmtree(Path(engine.vault.root) / "아폴로")
        ids = await _fill(engine, script, 8)
        _emerges(script, "문학", "소설과 시를 모은다", ids[:4], axis="주제 분야")
        await engine.subdivision.consider(PurePosixPath())
        return ids

    async def test_dissolving_a_level_moves_its_folders_up(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await self._two_levels(engine, script)
        _emerges(script, "소설", "장편과 단편", ids[:2], axis="갈래")
        await engine.subdivision.consider(PurePosixPath("문학"))
        assert (Path(engine.vault.root) / "문학" / "소설").is_dir()

        script.set_dissolve(["문학"])
        # Asked of the folder itself: a document filed at the root never reaches 문학,
        # and this operator is about the level, not about an arrival.
        await engine.subdivision.consider(PurePosixPath("문학"))

        root = Path(engine.vault.root)
        assert not (root / "문학").exists()
        assert (root / "소설").is_dir(), "그 안의 폴더는 한 단 위로 올라온다"

    async def test_a_level_that_holds_up_is_left_alone(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """KEEP is the default; nothing is dissolved unless it answers DISSOLVE."""
        await self._two_levels(engine, script)

        await engine.subdivision.consider(PurePosixPath("문학"))

        assert (Path(engine.vault.root) / "문학").is_dir()

    async def test_the_root_is_never_dissolved(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """It has nowhere to promote to, so the question is never even asked."""
        await self._two_levels(engine, script)
        script.set_dissolve([""])
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        asked = [p for p in llm.prompts_for(None) if "THE LEVEL IN QUESTION: \n" in p.user]
        assert not asked
        assert Path(engine.vault.root).is_dir()


class TestMovingIntoAFolderThatAlreadyStands:
    """Cobweb's merge, the half we did not have.

    Measured on 300 documents: the root held 금융 beside 가상자산, 벤처투자, 신용정보 and
    신용협동조합, and grouping asked five times to put them together. Every one was
    refused -- 금융·신용·투자 because a member restated it, 금융 because the name was
    taken, by the folder it wanted to move them into.
    """

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"], once=True)
            await engine.subdivision.consider(PurePosixPath())

    async def test_folders_move_inside_the_one_that_is_named(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="문학", sign="문학 자료"),
        )
        script.set_shelved(["역사"])

        await self._three_shelves(engine, script)

        assert (engine.vault.root / "문학/역사").is_dir()
        # It kept its own place, its own documents and its own note.
        assert (engine.vault.root / "문학/doc0.txt").is_file()
        assert (engine.vault.root / "문학/_folder.md").is_file()
        assert "문학" in (engine.vault.root / "문학/_folder.md").read_text(encoding="utf-8")
        assert not (engine.vault.root / "역사").exists()
        assert (engine.vault.root / "과학").is_dir()

    async def test_one_folder_is_enough_to_move(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Nothing is created, so there is no level to justify: the list just gets
        shorter by one. Building a new shelf still needs two."""
        result = validate_grouping(
            name="금융",
            axis="주제",
            members=("신용협동조합",),
            siblings=("금융", "신용협동조합", "과학기술", "소비자"),
            into_existing=True,
        )

        assert result.accepted

    async def test_it_still_cannot_swallow_the_whole_level(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="문학", sign="문학 자료"),
        )
        # 아폴로 is the seeded fixture folder, so with 문학 as the shelf this is the rest.
        script.set_shelved(["과학", "역사", "아폴로"])

        await self._three_shelves(engine, script)

        assert (engine.vault.root / "역사").is_dir()
        assert not (engine.vault.root / "문학/역사").exists()


class TestAShelfCanDivideAfterAll:
    """A folder built by grouping carries its parent's property on purpose -- the folders
    standing in it are answers to it. Holding that against the ancestors refused every
    class the shelf went on to propose: 76 refusals in one run, and 77 documents left
    loose in a shelf that could not divide."""

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"], once=True)
            await engine.subdivision.consider(PurePosixPath())

    async def test_a_settled_property_is_not_held_against_its_own_ancestors(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 10)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="인문", sign="문학과 역사"),
        )
        script.set_shelved(["문학", "역사"])
        await self._three_shelves(engine, script)
        assert (engine.vault.root / "인문").is_dir()
        # Grouping moves folders, so the shelf starts with no loose pile of its own; what
        # arrives afterwards is what it could grow a class from.
        script.set(placement_prompts.PlacementDecision, place_at("인문"))
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))
        for index in range(4):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")
        # The shelf carries the root's property, which is exactly the one a new class
        # inside it has to answer.
        _emerges(script, "철학", "철학 자료", ["D0001", "D0002"], axis="주제", once=True)

        await engine.subdivision.consider(PurePosixPath("인문"))

        assert (engine.vault.root / "인문/철학").is_dir()


class TestAShelfIsNotDissolvedTheMomentItIsBuilt:
    """Merge and split are reverse operators, so on unchanged evidence they undo each
    other. Measured: a shelf holding 52 files was grouped at 03:30:52 and dissolved at
    03:31:05, and the same happened again fourteen seconds apart elsewhere in the run."""

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"], once=True)
            await engine.subdivision.consider(PurePosixPath())

    async def test_the_reverse_waits_for_the_evidence_to_move(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="인문", sign="문학과 역사"),
        )
        script.set_shelved(["문학", "역사"])
        await self._three_shelves(engine, script)
        assert (engine.vault.root / "인문").is_dir()
        # Everything answers DISSOLVE from here on.
        script.set_dissolve(["인문"])

        await engine.subdivision.consider(PurePosixPath("인문"))

        assert (engine.vault.root / "인문").is_dir()
        assert (engine.vault.root / "인문/문학").is_dir()


class TestABroaderNameIsCheckedBeforeAnythingMoves:
    """Grouping is the one operator that invents a name without choosing a property, so
    nothing the axis check refuses ever reached it. Unchecked it put 283 of 300 documents
    behind a single folder called 개별 법률 및 시행령 -- true of almost every document in
    the collection, and so excluding nothing."""

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"], once=True)
            await engine.subdivision.consider(PurePosixPath())

    async def test_a_container_name_moves_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="개별 자료", sign="여러 자료"),
        )
        script.set_shelved(["문학", "역사"])
        script.set_shelf_is_container()

        await self._three_shelves(engine, script)

        assert not (engine.vault.root / "개별 자료").exists()
        assert (engine.vault.root / "문학").is_dir()

    async def test_it_is_asked_once_the_members_are_known(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """Shown nothing, the check passed a name that says what its contents are.

        It used to run before the membership loop, to save one call per folder standing
        beside the shelf. Answering about a bare name, it let 중소기업 지원 관련 법률
        through -- the exact shape it exists to refuse. The folders that would move are
        the evidence, so it waits for them.
        """
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="개별 자료", sign="여러 자료"),
        )
        script.set_shelved(["문학", "역사"])
        script.set_shelf_is_container()

        await self._three_shelves(engine, script)

        asked = [p for p in llm.prompts_for(None) if "THE BROADER NAME:" in p.user]
        assert asked
        assert "문학/" in asked[-1].user and "역사/" in asked[-1].user
        assert not (engine.vault.root / "개별 자료").exists()


class TestAShelfAnswersForWhatMovesInsideIt:
    """A folder already standing here was named before the newcomers existed, so nothing
    had ever asked whether its name covers them. Unasked, 과학기술 연구개발 및 기관 -- 42
    documents of research law -- was moved under 중앙행정기관 조직 및 직제, whose name then
    answered for a fifth of its own contents."""

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"], once=True)
            await engine.subdivision.consider(PurePosixPath())

    async def test_a_folder_the_standing_name_does_not_cover_stays_put(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 10)
        await self._three_shelves(engine, script)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="문학", sign="문학 자료"),
        )
        script.set_shelved(["과학"])
        script.set_name_is_beside()

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "과학").is_dir()
        assert not (engine.vault.root / "문학" / "과학").exists()

    async def test_a_new_shelf_is_not_asked_the_question(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """It is named from its members, so its name cannot fail to cover them."""
        await _fill(engine, script, 10)
        await self._three_shelves(engine, script)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="인문", sign="인문 자료"),
        )
        script.set_shelved(["문학", "역사"])
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        asked = [
            p for p in llm.prompts_for(None) if "THE FOLDER THAT WOULD MOVE INSIDE IT:" in p.user
        ]
        assert not asked


class TestALevelThatAnswersNothingGoesWithoutAsking:
    """One folder below and none of its own documents: every document under it is under
    its single child, so the reader pays a guess to reach a list of one. 전통시장 및
    지역경제 held ten documents that way through a run that asked the split question 273
    times."""

    async def test_a_pass_through_level_is_dissolved(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))
        # A level with nothing of its own, standing over the one folder that holds it all.
        (engine.vault.root / "인문").mkdir()
        shutil.move(str(engine.vault.root / "문학"), str(engine.vault.root / "인문" / "문학"))
        (engine.vault.root / "인문" / CHARTER_FILENAME).write_text(
            Charter(path=PurePosixPath("인문"), title="인문", purpose="인문 자료").to_markdown(),
            encoding="utf-8",
        )
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath("인문"))

        assert not [p for p in llm.prompts_for(None) if "THE LEVEL IN QUESTION:" in p.user]


class TestGroupingDoesNotRebuildWhatSplittingTookDown:
    """The other direction of the reverse-operator rule. Measured: the same shelf was
    grouped and dissolved fifteen times in one run, twice within three seconds, because
    one document arriving between the two answers counted as the evidence moving."""

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"])
            await engine.subdivision.consider(PurePosixPath())

    async def test_a_dissolved_shelf_is_not_built_again(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        await self._three_shelves(engine, script)
        # Dissolve one, which is what the reverse operator is for.
        script.set_dissolve(["문학"])
        await engine.subdivision._consider_split(  # type: ignore[attr-defined]
            PurePosixPath("문학"), filename="", on_progress=None
        )
        assert not (engine.vault.root / "문학").exists()
        # And now grouping asks for it back, by name. It is only asked after a division
        # succeeds, so one has to succeed.
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="문학", sign="문학 자료"),
        )
        script.set_shelved(["과학", "역사"])
        _emerges(script, "시가", "시가 자료", ["D0001", "D0002"])

        await engine.subdivision.consider(PurePosixPath())

        assert not (engine.vault.root / "문학").exists()


class TestTheOtherClockIsNotWiped:
    """The whole-collection pass records when it last drew the top of the tree on the
    root note, and this service rewrites that note on every root division -- eighteen
    times in one 300-document run, which is how the record went missing and the pass
    stopped firing."""

    async def test_a_root_division_keeps_what_the_redesign_wrote(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 6)
        root = engine.charters.load(PurePosixPath()) or Charter(path=PurePosixPath(), title="/")
        (Path(engine.vault.root) / "_folder.md").write_text(
            root.model_copy(update={"redrawn_at_documents": 42}).to_markdown(), encoding="utf-8"
        )
        _emerges(script, "문학", "문학 자료", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        after = engine.charters.load(PurePosixPath())
        assert after is not None
        assert after.divided, "the division did happen"
        assert after.redrawn_at_documents == 42
