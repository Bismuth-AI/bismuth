"""CREATE: what has grown in a folder, who belongs to it, and what that costs."""

from __future__ import annotations

import re
import shutil
from pathlib import Path, PurePosixPath

import pytest

from bismuth.container import Bismuth
from bismuth.domain.charter import Charter
from bismuth.ports.llm import Prompt
from bismuth.prompts import subdivision as subdivision_prompts
from tests.conftest import ScriptedModel
from tests.subdivision_helpers import _emerges, _fill, _traced
from tests.test_ingest import add


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
        _emerges(script, "문학", "문학 자료", ids[:2], once=True)

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
        _emerges(script, "문학", "문학 자료에 관한 상세 분석입니다. " * 30, ids[:2], once=True)

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided
        note = engine.charters.load(PurePosixPath("문학"))
        assert note is not None and note.purpose == "주제: 문학"

    async def test_a_sign_carrying_a_request_local_handle_is_not_written_to_disk(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Handles mean nothing outside their request, and one reached a public file."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "D0001과 D0003을 제외한 나머지 문학 자료", ids[:2], once=True)

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
        _emerges(script, "문학", "문학 자료", ids[:2], once=True)

        await engine.subdivision.consider(PurePosixPath())

        folders = [
            p.name
            for p in engine.vault.root.iterdir()
            if p.is_dir() and p.name not in ("_inbox", ".bismuth", "아폴로")
        ]
        assert folders == ["문학"]  # no sibling was invented to hold doc2 and doc3
        assert (engine.vault.root / "doc2.txt").is_file()

    async def test_one_look_can_only_produce_one_folder(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The structural half of the fix: a reply carries one name, so a single look
        cannot lay down a class and a bucket for what it did not cover."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2], once=True)

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided[0].created == (PurePosixPath("문학"),)

    async def test_the_sidecar_travels_with_its_document(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2], once=True)

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "문학/doc0.txt.md").is_file()
        assert not (engine.vault.root / "doc0.txt.md").exists()

    async def test_a_loose_document_can_reuse_an_existing_direct_sign(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2], once=True)
        # The first two files move, so doc2 is D0001 in the request-local view of the
        # round that follows. Routing is one closed F### choice per loose document
        # (ADR-0014), and it runs in the same visit: the pile is read for a new class
        # first, and only what no class wanted is offered to the signs already standing.
        script.set_routes({"D0001": "F001"})

        divided = await engine.subdivision.consider(PurePosixPath())

        # Two rounds in the one visit: the class comes out, then what it left behind is
        # offered to the sign it just raised.
        assert divided[0].created == (PurePosixPath("문학"),)
        assert divided[0].moved == 2
        assert all(one.routed and one.moved == 1 for one in divided[1:])
        assert (engine.vault.root / "문학/doc2.txt").is_file()
        assert (engine.vault.root / "문학/doc2.txt.md").is_file()
        # Everything the class left behind was offered the sign it raised, one document at
        # a time, so the pile is empty rather than one short.
        assert not list(engine.vault.root.glob("doc*.txt"))

    async def test_the_new_folder_gets_a_note_that_distinguishes_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "소설과 시. 과학 자료가 아닌 것.", ids[:2], once=True)

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath("문학"))
        assert charter is not None
        assert charter.purpose == "소설과 시. 과학 자료가 아닌 것."

    async def test_drawing_a_class_out_is_one_undoable_batch(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2], once=True)
        await engine.subdivision.consider(PurePosixPath())

        entry = next(e for e in engine.journal.iter_entries() if "divide" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "doc0.txt").is_file()
        assert (engine.vault.root / "doc1.txt").is_file()

    async def test_an_unusable_folder_name_is_skipped_not_fatal(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        _emerges(script, "...", "쓸 수 없는 이름", ids[:1], once=True)

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

        with _traced() as events:
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


class TestEveryMoveNamesItsDocument:
    async def test_a_swept_document_says_where_it_went(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """subdivide.applied carries a count, and its document_id is the arrival that
        triggered the pass. On a 165-document vault 186 of 205 moves were unattributable,
        so "why is this document here" had no answer for nine documents in ten."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "소설과 시에 관한 자료", ids[:2], once=True)

        with _traced() as events:
            await engine.subdivision.consider(PurePosixPath())

        moves = [f for e, f in events if e == "document.moved"]
        assert len(moves) == 2
        assert {m["document_id"] for m in moves} == {"D0001", "D0002"}
        assert all(m["to_folder"] == "문학" for m in moves)


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
        _emerges(script, "문학", "소설과 시를 모은다", ids[:2], axis="주제 분야", once=True)
        await engine.subdivision.consider(PurePosixPath())
        charter = engine.charters.load(PurePosixPath())
        assert charter is not None and charter.split_basis == "주제 분야"
        llm.calls.clear()

        # A new arrival, so the loose pile has changed and the folder is asked again.
        _emerges(script, "과학", "실험과 관측을 모은다", ids[2:4], axis="완전히 다른 축", once=True)
        await add(engine, "doc99.txt", "문서 99 내용")

        assert not llm.prompts_for(subdivision_prompts.Axis)

    async def test_the_sign_is_written_without_being_shown_the_name(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "소설과 시를 모은다", ids[:2], axis="주제 분야", once=True)
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
        _emerges(script, "문학", "소설과 시를 모은다", ids[:2], axis="주제 분야", once=True)
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()

        _emerges(script, "과학", "실험과 관측을 모은다", ids[2:4], axis="주제 분야", once=True)
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


class TestTheLanguageInstruction:
    """SPEC 2.1: an instruction is read when it is generated, and the reply is last."""

    @pytest.mark.parametrize(
        "prompt",
        [
            subdivision_prompts.build_group(
                documents=[("a", "ko doc")], children=[], language="ko"
            ),
            subdivision_prompts.build_axis(
                shared="같은 법령을 다룬다", rest=["금융", "소비자"], language="ko"
            ),
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
        prompt = subdivision_prompts.build_axis(shared="one subject", rest=[])

        assert "These documents are written in" not in prompt.user


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
