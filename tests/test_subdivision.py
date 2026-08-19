"""Drawing a class out of a folder: when it happens, what it moves, what it leaves."""

from __future__ import annotations

import re
import shutil
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest
from pydantic import ValidationError

from bismuth.container import Bismuth
from bismuth.domain.charter import Charter
from bismuth.domain.maintenance import validate_grouping
from bismuth.ports.llm import Prompt
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import subdivision as subdivision_prompts
from bismuth.services import subdivision as subdivision_service
from tests.conftest import ScriptedModel
from tests.test_ingest import add, place_at


def _emerges(
    script: ScriptedModel, name: str, note: str, ids: list[str], *, axis: str = "주제"
) -> None:
    """Script a class coming out of the pile: its name, then who belongs to it."""
    script.set(
        subdivision_prompts.Emerging,
        subdivision_prompts.Emerging(
            emerged=True,
            axis=axis,
            axis_question=f"어느 {axis}에 속하는가?",
            name=name,
            sign=note,
        ),
    )
    script.set(
        subdivision_prompts.Members,
        subdivision_prompts.Members(document_ids=ids),
    )
    # Membership is one closed SHELF/STAY choice per document since ADR-0014; the
    # Members schema above no longer reaches this path.
    script.set_members(ids)


def _by_name(engine: Bismuth) -> dict[str, str]:
    return {
        source.filename: document_id
        for document_id, _ in engine.catalog.iter_cards()
        if (source := engine.catalog.load_source(document_id)) is not None
    }


async def _fill(engine: Bismuth, script: ScriptedModel, count: int) -> list[str]:
    """Put documents in root and return the short handles shown in one maintenance view."""
    script.set(placement_prompts.PlacementDecision, place_at(""))
    for index in range(count):
        await add(engine, f"doc{index}.txt", f"문서 {index} 내용")
    return [f"D{index:04d}" for index in range(1, count + 1)]


class TestDivideDecision:
    async def test_model_prompts_use_request_local_handles_not_catalog_hashes(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        await _fill(engine, script, 4)

        prompts = llm.prompts_for(subdivision_prompts.Gathered)
        assert prompts
        assert all(not re.search(r"\[[0-9a-f]{16}(?:~\d+)?\]", item.user) for item in prompts)
        assert "[D0001]" in prompts[-1].user

    async def test_membership_is_one_bounded_question_per_document(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        """Output size is constant in the archive: one closed choice, one document.

        This replaced a packeted call returning a list of ids, where a long enough list
        could omit or duplicate one (ADR-0014).
        """
        documents = [(f"D{index:04d}", f"문서 {index}") for index in range(1, 31)]
        script.set_members([f"D{index:04d}" for index in range(1, 11)])

        result = await engine.maintenance._find_members(  # type: ignore[attr-defined]
            folder=PurePosixPath(),
            purpose="자료",
            documents=documents,
            name="자료",
        )

        calls = llm.prompts_for(None)
        assert len(calls) == len(documents)
        # One document per question: a reply can name no other document, in any archive.
        assert all(len(re.findall(r"\[(D\d{4})\]", call.user)) == 1 for call in calls)
        assert result.document_ids == [f"D{index:04d}" for index in range(1, 11)]

    async def test_nothing_happens_when_nothing_has_gathered(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The scripted default is "nothing emerged"; a young folder stays flat."""
        await _fill(engine, script, 4)

        assert not (engine.vault.root / "문학").exists()
        assert (engine.vault.root / "doc0.txt").is_file()

    async def test_an_empty_folder_is_never_asked_about(
        self,
        engine: Bismuth,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        await engine.subdivision.consider(PurePosixPath())
        assert not llm.prompts_for(subdivision_prompts.Emerging)

    async def test_the_inbox_is_not_a_category(self, engine: Bismuth, llm) -> None:  # type: ignore[no-untyped-def]
        await engine.subdivision.consider(PurePosixPath("_inbox"))
        assert not llm.prompts_for(subdivision_prompts.Emerging)

    async def test_a_human_written_note_is_left_alone(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """`managed: false` means a person arranged this; it is not ours to redraw."""
        await _fill(engine, script, 2)
        note = Charter(path=PurePosixPath(), title="내 서재", purpose="직접 정리함", managed=False)
        (engine.vault.root / "_folder.md").write_text(note.to_markdown(), encoding="utf-8")
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        assert not llm.prompts_for(subdivision_prompts.Emerging)

    async def test_naming_a_class_but_claiming_nobody_creates_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", [])

        assert await engine.subdivision.consider(PurePosixPath()) == []
        assert not (engine.vault.root / "문학").exists()


class TestDrawingOutAClass:
    async def test_the_documents_of_that_class_move_into_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert len(divided) == 1
        assert divided[0].moved == 2
        assert (engine.vault.root / "문학").is_dir()
        # Existing documents are re-filed, not just future ones (SPEC.md 3.4).
        assert not (engine.vault.root / "doc0.txt").exists()

    async def test_a_proposal_that_translates_the_corpus_is_rejected(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(
            script,
            "Safety and Occupational Health",
            "Regulations concerning workplace safety and occupational health.",
            ids[:2],
            axis="subject domain",
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert not (engine.vault.root / "Safety and Occupational Health").exists()

    async def test_an_essay_length_folder_note_falls_back_to_derived_state(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """An unusable sign degrades to the derived one; it never fails an ingest."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료에 관한 상세 분석입니다. " * 30, ids[:2])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided
        note = engine.charters.load(PurePosixPath("문학"))
        assert note is not None and note.purpose == "주제: 문학"

    async def test_a_sign_carrying_a_request_local_handle_is_not_written_to_disk(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Handles mean nothing outside their request, and one reached a public file."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "D0001과 D0003을 제외한 나머지 문학 자료", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        note = engine.charters.load(PurePosixPath("문학"))
        assert note is not None and note.purpose == "주제: 문학"

    async def test_the_rest_stay_and_are_given_no_name(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The whole point. A partition has to account for every document, so the
        leftovers get a folder called "everything else"; drawing one class out cannot
        express that, and SPEC.md 3.4 says they stay in the parent."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        folders = [
            p.name
            for p in engine.vault.root.iterdir()
            if p.is_dir() and p.name not in ("_inbox", ".bismuth", "아폴로")
        ]
        assert folders == ["문학"]  # no sibling was invented to hold doc2 and doc3
        assert (engine.vault.root / "doc2.txt").is_file()
        assert (engine.vault.root / "doc3.txt").is_file()

    async def test_one_look_can_only_produce_one_folder(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The structural half of the fix: a reply carries one name, so a single look
        cannot lay down a class and a bucket for what it did not cover."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided[0].created == (PurePosixPath("문학"),)

    async def test_the_sidecar_travels_with_its_document(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "문학/doc0.txt.md").is_file()
        assert not (engine.vault.root / "doc0.txt.md").exists()

    async def test_a_loose_document_can_reuse_an_existing_direct_sign(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        # The first two files have moved, so doc2 is D0001 in this new request-local
        # view rather than its earlier D0003. Routing is one closed F### choice per
        # loose document (ADR-0014).
        script.set_routes({"D0001": "F001"})
        # Nothing new emerges, which is what leaves the loose document to routing: the
        # pile is read for a new class first, and only what no class wanted is offered
        # to the signs already standing.
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided[0].created == (PurePosixPath("문학"),)
        assert divided[0].moved == 1
        assert (engine.vault.root / "문학/doc2.txt").is_file()
        assert (engine.vault.root / "문학/doc2.txt.md").is_file()
        assert (engine.vault.root / "doc3.txt").is_file()

    async def test_the_new_folder_gets_a_note_that_distinguishes_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "소설과 시. 과학 자료가 아닌 것.", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath("문학"))
        assert charter is not None
        assert charter.purpose == "소설과 시. 과학 자료가 아닌 것."

    async def test_drawing_a_class_out_is_one_undoable_batch(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])
        await engine.subdivision.consider(PurePosixPath())

        entry = next(e for e in engine.journal.iter_entries() if "divide" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "doc0.txt").is_file()
        assert (engine.vault.root / "doc1.txt").is_file()

    async def test_an_unusable_folder_name_is_skipped_not_fatal(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        _emerges(script, "...", "쓸 수 없는 이름", ids[:1])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert (engine.vault.root / "doc0.txt").is_file()


class TestRemembering:
    async def test_the_folder_records_what_it_was_divided_along(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Without this the only question available later is "how would you divide
        this", which has an answer every time and so never settles."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath())
        assert charter is not None
        assert charter.split_basis == "주제"  # the axis, not this one extraction
        assert charter.split_at_documents == 4
        assert charter.divided


class TestTermination:
    async def test_a_class_that_takes_everything_is_not_a_division(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """It only moves the folder a level deeper, leaving the same problem at the same
        size -- which recurses for ever."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "전부", ids)

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert not (engine.vault.root / "문학").exists()
        assert (engine.vault.root / "doc0.txt").is_file()

    async def test_what_was_just_created_is_not_immediately_re_judged(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The new folder was formed a moment ago from a judgement over these same
        documents; asking it again adds no evidence, only depth. With the recursion in,
        one ingest built 철학/현상학/체화된 인지, a document per level. It is asked as soon
        as anything lands in it, which is what actually counts as new evidence."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert len(divided) == 1
        assert (engine.vault.root / "문학/doc0.txt").is_file()
        assert (engine.vault.root / "문학/doc1.txt").is_file()
        assert not (engine.vault.root / "문학/문학").exists()

    async def test_a_class_may_not_carry_its_parents_name(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Asked what has grown in 철학 the model answers 철학 -- true, and useless. It
        is caught below only when the class takes everything; taking three of five is
        how 철학/철학 and 과학·기술 연구/과학·기술 연구 were built."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())

        # Now 문학 holds two, and the model proposes 문학 again for one of them.
        _emerges(script, "문학", "또 문학", ids[:1])
        divided = await engine.subdivision.consider(PurePosixPath("문학"))

        assert divided == []
        assert not (engine.vault.root / "문학/문학").exists()


class TestOneAxisPerFolder:
    """Sub-folders of one folder are answers to one question. Without that, siblings sit
    on different distinctions and no name rules anything out -- measured on 300 legal
    documents as 주제 (과학기술), 문서 종류 (시행규칙) and individual statute names side by
    side at the same root."""

    async def test_the_axis_is_recorded_when_the_first_class_comes_out(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2], axis="주제 분야")

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath())
        assert charter is not None
        assert charter.split_basis == "주제 분야"

    async def test_a_divided_folder_is_held_to_the_axis_it_has(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2], axis="주제 분야")
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()

        _emerges(script, "과학", "과학 자료", ids[2:], axis="완전히 다른 축")
        await engine.subdivision.consider(PurePosixPath())

        # The second look is told the axis rather than asked for one...
        asked = llm.prompts_for(subdivision_prompts.Gathered)[-1]
        assert "주제 분야" in asked.user
        # ...and the folder keeps it, whatever the reply says.
        charter = engine.charters.load(PurePosixPath())
        assert charter is not None
        assert charter.split_basis == "주제 분야"

    async def test_redrawing_a_note_does_not_erase_the_axis(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Notes are redrawn every time a document lands, and before subdivision runs.
        A redraft that dropped the history erased the axis on the way in, so it survived
        only until the next arrival -- which is most of why sibling folders drifted."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2], axis="주제 분야")
        await engine.subdivision.consider(PurePosixPath())
        before = engine.charters.load(PurePosixPath("문학"))
        assert before is not None

        script.set(placement_prompts.PlacementDecision, place_at("문학"))
        await add(engine, "another.txt", "문학 문서 하나 더")

        after = engine.charters.load(PurePosixPath("문학"))
        assert after is not None
        assert after.split_basis == before.split_basis
        assert after.split_at_documents == before.split_at_documents


class TestAnAxisIsSpentOnce:
    """Within a folder, every ancestor's axis has one constant value -- that is what put
    these documents together -- so none of them can separate anything here. Measured
    without this: 시행규칙/과학기술정보통신부 소관/시행규칙, and beside it 시행령, which reads
    as though an enforcement rule could contain an enforcement decree."""

    async def _divide_root(self, engine: Bismuth, script: ScriptedModel) -> list[str]:
        ids = await _fill(engine, script, 6)
        _emerges(script, "법률", "법률", ids[:4], axis="법령의 종류")
        await engine.subdivision.consider(PurePosixPath())
        return ids

    async def test_an_axis_used_above_is_refused(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await self._divide_root(engine, script)
        script.set(placement_prompts.PlacementDecision, place_at("법률"))
        await add(engine, "more.txt", "법률 문서 하나 더")

        # The child proposes the axis its parent was already divided along.
        _emerges(script, "시행령", "시행령", ids[:1], axis="법령의 종류")
        divided = await engine.subdivision.consider(PurePosixPath("법률"))

        assert divided == []
        assert not (engine.vault.root / "법률/시행령").exists()

    async def test_a_different_axis_below_is_fine(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await self._divide_root(engine, script)

        _emerges(script, "금융위원회", "금융위원회 소관", ids[:2], axis="소관 부처")
        divided = await engine.subdivision.consider(PurePosixPath("법률"))

        assert (engine.vault.root / "법률/금융위원회").is_dir()
        assert divided[0].basis == "소관 부처"

    async def test_a_class_may_not_carry_a_grandparents_name(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await self._divide_root(engine, script)
        _emerges(script, "금융위원회", "금융위원회 소관", ids[:2], axis="소관 부처")
        await engine.subdivision.consider(PurePosixPath("법률"))

        # Two levels down, proposing the name of the top-level folder.
        _emerges(script, "법률", "법률", ids[:1], axis="문서 성격")
        divided = await engine.subdivision.consider(PurePosixPath("법률/금융위원회"))

        assert divided == []
        assert not (engine.vault.root / "법률/금융위원회/법률").exists()


class TestTheAxisStaysSingleLine:
    def test_multiline_state_is_not_an_axis(self) -> None:
        with pytest.raises(ValidationError):
            subdivision_prompts.Division(basis="one\ntwo")
        with pytest.raises(ValidationError):
            subdivision_prompts.Emerging(emerged=True, axis="one\ntwo", name="x")


class TestAFolderBornFull:
    async def test_a_new_shelf_is_asked_about_itself_once(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """Its documents were moved in, not filed in, so nothing else would ask it."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학", ids[:4])
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        # The shelf holds four of the six; the root keeps two. Neither prompt carries a
        # folder name any more, so the one that is about the shelf is the one showing
        # the documents that moved into it.
        asked = [
            p for p in llm.prompts_for(subdivision_prompts.Gathered) if "DOCUMENTS (4)" in p.user
        ]
        assert asked


class TestStandingFoldersTogether:
    """The fourth operation: a level that grew too wide can be narrowed again."""

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        """Handles are request-local, so the two documents still loose are always D0001-2."""
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"])
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


class TestWhereTheDocumentsWent:
    """Descending is decided by whether a shelf emptied its parent, not by depth."""

    async def test_a_thin_shelf_is_not_descended_into(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """It left a pile behind, and the pile is the more urgent question."""
        await _fill(engine, script, 8)
        _emerges(script, "문학", "문학 자료", ["D0001", "D0002"])
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "문학").is_dir()
        assert not [p for p in llm.prompts_for(subdivision_prompts.Emerging) if "문학/" in p.user]

    async def test_a_shelf_that_emptied_its_parent_is_asked_again(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """The whole problem moved one level down; nothing else would ask it."""
        await _fill(engine, script, 8)
        _emerges(script, "문학", "문학 자료", [f"D{index:04d}" for index in range(1, 7)])
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        # Six of the eight moved down, so the shelf is the prompt showing six.
        asked = [
            p for p in llm.prompts_for(subdivision_prompts.Gathered) if "DOCUMENTS (6)" in p.user
        ]
        assert asked


class TestARefusedSignSaysWhy:
    async def test_the_fallback_note_is_recorded_with_its_reason(
        self, engine: Bismuth, script: ScriptedModel, caplog
    ) -> None:
        """The fallback repeats the folder name and rules nothing out, so a run that
        writes it often has a defect -- and only the finished vault showed it before."""
        ids = await _fill(engine, script, 6)
        # A sign that is the folder name again is one of the four refusals.
        _emerges(script, "문학", "문학", ids[:2])

        events = []
        with mock.patch.object(
            subdivision_service, "log_trace", lambda e, **f: events.append((e, f))
        ):
            await engine.subdivision.consider(PurePosixPath())

        refusals = [f for e, f in events if e == "subdivide.sign_refused"]
        assert refusals
        assert refusals[0]["reason"] == "sign is the folder name again"
        assert refusals[0]["name"] == "문학"


class TestMembershipSeesTheSign:
    async def test_the_scope_line_reaches_the_membership_question(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        """Deciding from a two-word name is deciding from what the words suggest. One
        folder named 이공계인력지원 collected 가상융합산업 진흥법 because that law
        mentions training specialists, and could never be divided again."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "소설과 시, 그리고 문학 비평에 관한 자료", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        asked = [p for p in llm.prompts_for(None) if "NEW SIGN:" in p.user]
        assert asked
        assert all("소설과 시, 그리고 문학 비평에 관한 자료" in p.user for p in asked)


class TestRoutingSeesTheNotes:
    async def test_each_existing_sign_carries_its_note(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        """Shown names only, routing put 가상자산 이용자 보호법 시행령 into 데이터 산업
        관련 법령 -- crypto reads as digital from a two-word name, and the note that
        ruled it out was not in the prompt."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "소설과 시. 과학 자료가 아닌 것.", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        routing = [p for p in llm.prompts_for(None) if "\n  [F" in p.user and "SIGNS:" in p.user]
        assert routing
        assert all("소설과 시" in p.user for p in routing)


class TestEveryMoveNamesItsDocument:
    async def test_a_swept_document_says_where_it_went(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """subdivide.applied carries a count, and its document_id is the arrival that
        triggered the pass. On a 165-document vault 186 of 205 moves were unattributable,
        so "why is this document here" had no answer for nine documents in ten."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "소설과 시에 관한 자료", ids[:2])

        events = []
        with mock.patch.object(
            subdivision_service, "log_trace", lambda e, **f: events.append((e, f))
        ):
            await engine.subdivision.consider(PurePosixPath())

        moves = [f for e, f in events if e == "document.moved"]
        assert len(moves) == 2
        assert {m["document_id"] for m in moves} == {"D0001", "D0002"}
        assert all(m["to_folder"] == "문학" for m in moves)


class TestTheNameIsAnAnswerNotTheQuestion:
    """The failure the chain was rebuilt around, twice.

    Emergence used to ask one call for the class, its name, the folder's axis and that
    axis as a question. Hiding the enclosing folder's name from the naming step stopped
    it being echoed there -- and the axis step, which was still asked from the name it
    had just produced, handed the same string straight back: "1인 창조기업 육성" as both
    the name and the property, 72 times in 80, and validate_plan refused every one of
    them for repeating the axis instead of answering it. Nothing was built in 75
    documents.

    So neither step may see what the other must not repeat. The question is asked from
    the group's own sentence, and the name is asked as an answer to that question.
    """

    async def test_the_question_is_asked_before_any_name_exists(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 4)
        # A note that does not contain the name, so the name is the only thing the
        # assertion can be seeing.
        _emerges(script, "문학", "소설과 시를 모은다", ids[:2], axis="주제 분야")
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        asked = llm.prompts_for(subdivision_prompts.Axis)
        assert asked
        assert all("문학" not in prompt.user for prompt in asked)

    async def test_the_naming_step_is_given_a_question_and_not_the_property(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2], axis="주제 분야")
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        asked = llm.prompts_for(subdivision_prompts.ClassName)
        assert asked
        # The question is there to be answered; the property phrase on its own is what
        # came back as the name, so it is not offered.
        assert all("어느 주제 분야에 속하는가?" in prompt.user for prompt in asked)

    async def test_a_divided_folder_hands_its_own_question_to_the_naming_step(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """One path for both: inherited or new, the name answers a question."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2], axis="주제 분야")
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()

        _emerges(script, "과학", "과학 자료", ids[2:4], axis="주제 분야")
        await engine.subdivision.consider(PurePosixPath())

        assert not llm.prompts_for(subdivision_prompts.Axis)
        named = llm.prompts_for(subdivision_prompts.ClassName)
        assert named
        assert all("어느 주제 분야에 속하는가?" in prompt.user for prompt in named)


class TestNothingIsAskedTwice:
    """Every call in this class was made, answered, and thrown away in one 300-document
    round -- 145 axes, 32 signs and 84 groups, none of which could have been used."""

    async def test_a_folder_with_a_recorded_axis_is_not_asked_for_another(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """One child used to mean the axis was not settled, so it was asked again on every
        arrival and refused for repeating an ancestor's."""
        # The seeded folder would make the root two-childed and settle the axis for the
        # wrong reason; this test is about the folder that has exactly one.
        shutil.rmtree(Path(engine.vault.root) / "아폴로")
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "소설과 시를 모은다", ids[:2], axis="주제 분야")
        await engine.subdivision.consider(PurePosixPath())
        charter = engine.charters.load(PurePosixPath())
        assert charter is not None and charter.split_basis == "주제 분야"
        llm.calls.clear()

        # A new arrival, so the loose pile has changed and the folder is asked again.
        _emerges(script, "과학", "실험과 관측을 모은다", ids[2:4], axis="완전히 다른 축")
        await add(engine, "doc99.txt", "문서 99 내용")

        assert not llm.prompts_for(subdivision_prompts.Axis)

    async def test_the_sign_is_written_without_being_shown_the_name(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "소설과 시를 모은다", ids[:2], axis="주제 분야")
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        asked = llm.prompts_for(subdivision_prompts.ClassSign)
        assert asked
        assert all("문학" not in prompt.user for prompt in asked)

    async def test_the_group_is_given_its_bounds_as_numbers(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """ "At least two, never all of them" was ignored 84 times in 567 replies."""
        await _fill(engine, script, 5)

        asked = llm.prompts_for(subdivision_prompts.Gathered)
        assert asked
        assert any("BETWEEN 2 AND 4 OF THEM. NEVER ALL 5." in prompt.user for prompt in asked)

    async def test_an_inherited_axis_arrives_with_its_question(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """The property and its question are one answer and have to travel together.

        Split, a folder with one child inherited the property, which made the chain skip
        the step that writes the question, and the plan was refused for having no
        question -- 69 times in 108 documents, one folder built.
        """
        shutil.rmtree(Path(engine.vault.root) / "아폴로")
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "소설과 시를 모은다", ids[:2], axis="주제 분야")
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()

        _emerges(script, "과학", "실험과 관측을 모은다", ids[2:4], axis="주제 분야")
        await add(engine, "doc99.txt", "문서 99 내용")

        named = llm.prompts_for(subdivision_prompts.ClassName)
        assert named
        assert all("어느 주제 분야에 속하는가?" in prompt.user for prompt in named)


class TestTheCheapQuestionsComeFirst:
    """Membership is one closed question per document, so it is the loop that has to run
    last.

    Measured over one 300-document round: 2,446 membership questions, 2,060 of them --
    84% -- spent on proposals refused afterwards. What refuses them now is a string
    comparison, which is free, so the free checks go first and the loop is only reached
    by a name that has already survived them.
    """

    async def test_a_boundary_that_holds_still_asks_every_document(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """The loop was moved, not removed."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "소설과 시를 모은다", ids[:2], axis="주제 분야")
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        asked = [prompt for prompt in llm.prompts_for(None) if "NEW SIGN:" in prompt.user]
        assert asked

    async def test_a_name_code_can_refuse_never_reaches_the_loop(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """Whether a name repeats its axis is a string comparison; membership is one
        model call per document. Run the other way round, 76 proposals in one round were
        asked about before code refused them on the name alone."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "주제 분야", "소설과 시를 모은다", ids[:2], axis="주제 분야")
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        asked = [prompt for prompt in llm.prompts_for(None) if "NEW SIGN:" in prompt.user]
        assert not asked


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


class TestTheLanguageInstruction:
    """SPEC 2.1: an instruction is read when it is generated, and the reply is last."""

    @pytest.mark.parametrize(
        "prompt",
        [
            subdivision_prompts.build_group(
                documents=[("a", "ko doc")], children=[], language="ko"
            ),
            subdivision_prompts.build_axis(shared="같은 법령을 다룬다", language="ko"),
            subdivision_prompts.build_class_sign(
                shared="같은 법령을 다룬다", documents=[("a", "ko doc")], language="ko"
            ),
            subdivision_prompts.build_class_name(
                shared="같은 법령을 다룬다",
                question="이 문서의 규제 대상은?",
                documents=[("a", "ko doc")],
                taken=[],
                language="ko",
            ),
            subdivision_prompts.build_emerging(
                path="", purpose="", documents=[("a", "ko doc")], children=[], language="ko"
            ),
            subdivision_prompts.build_grouping(
                path="", children=[("문학", "", 3)], axis="주제", language="ko"
            ),
        ],
    )
    def test_it_comes_before_the_evidence(self, prompt: Prompt) -> None:
        """Six of thirteen replies came back in English with this line at the end."""
        assert prompt.user.startswith("These documents are written in `ko`")

    def test_a_collection_with_no_language_is_told_nothing(self) -> None:
        """The code comes off the cards, so this file never names a language itself."""
        prompt = subdivision_prompts.build_axis(shared="one subject")

        assert "These documents are written in" not in prompt.user


class TestThePropertyIsCheckedOnce:
    """Task #31. The one judgement that outlives every later question about a folder.

    It lived inside the boundary audit, refused 118 axes in one 300-document run, and was
    deleted with the audit. The next run fixed the root on 법률명 -- one folder per law,
    which excludes nothing -- at 89 documents.
    """

    async def test_a_refused_property_builds_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 6)
        _emerges(script, "시행령", "시행령 문서", ids[:2], axis="문서 종류")
        script.set_axis_fails()

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert not (Path(engine.vault.root) / "시행령").exists()

    async def test_a_divided_folder_does_not_pay_for_it_again(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """The axis is recorded after the first division, and every later class answers a
        question that has already been checked."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()
        _emerges(script, "과학", "과학 자료", ids[2:4])

        await engine.subdivision.consider(PurePosixPath())

        asked = [p for p in llm.prompts_for(None) if "QUESTION IT ASKS: " in p.user]
        assert not asked


class TestTheSignIsWrittenFromDocuments:
    """Task #32. Given one word, the sign step invented a folder nothing could join.

    Measured: shared="중소벤처기업부" produced "중소기업 및 벤처기업 지원 정책과 사업
    공고가 담긴 문서" over documents that were the full text of 시행규칙, and all 5,125
    membership answers were STAY.
    """

    async def test_the_documents_are_in_the_prompt(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        signs = llm.prompts_for(subdivision_prompts.ClassSign)
        assert signs
        assert "THEM:" in signs[-1].user
        assert "[D0001]" in signs[-1].user

    def test_the_name_is_still_kept_out(self) -> None:
        """The reason the sign never sees the name: it came back as the name 22 times in
        349, which is the one string it must not produce."""
        prompt = subdivision_prompts.build_class_sign(
            shared="같은 법령을 다룬다", documents=[("D0001", "어떤 문서")]
        )

        assert "문학" not in prompt.user


class TestANameThatShelvedNothing:
    """Task #30. Nothing recorded a proposal that bought nothing, so it came back.

    중소벤처기업부 was proposed 55 times in one folder and asked 5,125 membership
    questions, shelving no document at all. Three such names took 9,606 of the run's
    10,512 membership questions.
    """

    async def test_it_is_not_asked_about_again(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 6)
        # Named, but nobody claims it: the loop runs once and shelves nothing.
        _emerges(script, "문학", "문학 자료", ids[:2])
        script.set_members([])
        await engine.subdivision.consider(PurePosixPath())
        first = [p for p in llm.prompts_for(None) if "NEW SIGN: 문학/" in p.user]
        assert first, "the first proposal must reach the membership loop"
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        again = [p for p in llm.prompts_for(None) if "NEW SIGN: 문학/" in p.user]
        assert not again

    async def test_another_name_is_still_free_to_ask(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """The memory is about one name at one folder, not about the folder."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])
        script.set_members([])
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()
        _emerges(script, "과학", "과학 자료", ids[2:4])

        await engine.subdivision.consider(PurePosixPath())

        assert [p for p in llm.prompts_for(None) if "NEW SIGN: 과학/" in p.user]


class TestMovingIntoAFolderThatAlreadyStands:
    """Cobweb's merge, the half we did not have.

    Measured on 300 documents: the root held 금융 beside 가상자산, 벤처투자, 신용정보 and
    신용협동조합, and grouping asked five times to put them together. Every one was
    refused -- 금융·신용·투자 because a member restated it, 금융 because the name was
    taken, by the folder it wanted to move them into.
    """

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"])
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
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"])
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
        _emerges(script, "철학", "철학 자료", ["D0001", "D0002"], axis="주제")

        await engine.subdivision.consider(PurePosixPath("인문"))

        assert (engine.vault.root / "인문/철학").is_dir()


class TestAShelfIsNotDissolvedTheMomentItIsBuilt:
    """Merge and split are reverse operators, so on unchanged evidence they undo each
    other. Measured: a shelf holding 52 files was grouped at 03:30:52 and dissolved at
    03:31:05, and the same happened again fourteen seconds apart elsewhere in the run."""

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"])
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
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"])
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

    async def test_it_is_asked_before_the_folders_are(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """The membership loop costs one call per folder standing here."""
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="개별 자료", sign="여러 자료"),
        )
        script.set_shelf_is_container()

        await self._three_shelves(engine, script)

        assert not [p for p in llm.prompts_for(None) if "THE BROADER SHELF:" in p.user]


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
