"""The property a folder is fixed on, and the names it is allowed to produce."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from bismuth.container import Bismuth
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import subdivision as subdivision_prompts
from tests.conftest import ScriptedModel
from tests.subdivision_helpers import _emerges, _fill
from tests.test_ingest import add, place_at


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
        # Eight, so that four are still loose after the first class comes out and the
        # second look is a question the gate lets through.
        ids = await _fill(engine, script, 8)
        _emerges(script, "문학", "문학 자료", ids[:2], axis="주제 분야")
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()

        _emerges(script, "과학", "과학 자료", ids[2:4], axis="완전히 다른 축")
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


class TestNamingTheLawIsAllowedBelowTheRoot:
    """The two prompts disagreed. The generator is told never to ask a question every
    document answers differently -- right at the root, where 법률명 gives every folder one
    document -- while the checker carries an exception it never saw: below the root, one
    law's act, decree and rules belong on one shelf.

    So inside a subject folder the generator could not produce an answer its checker would
    accept, and it routed around the rule: shown 과학관법's three documents it answered
    과학관 관련 법률 여부, a yes/no about that one law, and the check refused it. 60 of one
    run's 64 axis refusals were below the root, and 40 carried a law's name as the class."""

    async def test_the_root_is_not_told_it_may(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        asked = llm.prompts_for(subdivision_prompts.Axis)
        assert asked
        assert "WHICH work a document belongs to" not in asked[0].system

    async def test_a_subject_folder_is(self, engine: Bismuth, script: ScriptedModel, llm) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 8)
        _emerges(script, "문학", "문학 자료", ids[:4])
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()
        # A class emerging inside the subject folder, which is where the exception applies.
        _emerges(script, "시집", "시집 자료", ["D0001", "D0002"])

        await engine.subdivision.consider(PurePosixPath("문학"))

        asked = llm.prompts_for(subdivision_prompts.Axis)
        assert asked
        assert "WHICH work a document belongs to" in asked[-1].system


class TestTheAxisCheckSeesTheFolderItJudges:
    """Two of its rules are about whether the documents here would give different answers
    to the property, and the request carried a path and a label.

    Measured on 300 documents: it held 상생협력 촉진 분야 over a folder of 과학관법,
    디지털포용 and 가상융합산업 -- a well-formed subject property that nothing in that
    folder answers -- and six folders were fixed on it, one of them left with 55
    documents it could never divide."""

    async def test_what_the_folder_is_about_is_in_the_question(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        asked = [p for p in llm.prompts_for(None) if "QUESTION IT ASKS:" in p.user]
        assert asked
        assert "WHAT THE DOCUMENTS HERE ARE ABOUT:" in asked[0].user

    async def test_a_settled_axis_is_not_re_checked(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """It is asked once, when the property is chosen, and never again."""
        ids = await _fill(engine, script, 8)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()
        _emerges(script, "역사", "역사 자료", ids[2:4])

        await engine.subdivision.consider(PurePosixPath())

        assert not [p for p in llm.prompts_for(None) if "QUESTION IT ASKS:" in p.user]


class TestAPileThatSaysItIsAllOneThingIsAskedAgain:
    """The answer breaks the one contract the step has -- a group must leave a remainder
    -- so the guard refuses it, and a refused answer is not a finding about the folder.

    Remembered as one it locked the biggest piles out of being asked at all: measured on
    300 documents, 67 divisions blocked to save 8 calls, and every folder the spec counted
    as an undivided pile was one this memory had shut. The root reached 103 loose documents
    without being asked once after it answered at 61."""

    @staticmethod
    def _takes_everything(script: ScriptedModel) -> None:
        """Answer with every handle shown, which is what the guard is about."""
        script.set(
            subdivision_prompts.Gathered,
            lambda prompt, schema: subdivision_prompts.Gathered(
                members=[
                    line.strip()[1:6]
                    for line in prompt.user.splitlines()
                    if line.strip().startswith("[D")
                ],
                shared="모두 같은 종류의 문서",
            ),
        )

    async def test_the_next_arrival_asks_again(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        await _fill(engine, script, 6)
        self._takes_everything(script)
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()
        await add(engine, "새문서.txt", "새로 들어온 문서 내용")

        await engine.subdivision.consider(PurePosixPath())

        assert llm.prompts_for(subdivision_prompts.Gathered)

    async def test_a_group_that_takes_everything_still_builds_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Refused, every time. Cheap: the chain stops at the grouping call."""
        await _fill(engine, script, 6)
        self._takes_everything(script)

        divided = await engine.subdivision.consider(PurePosixPath())

        assert not divided


class TestANameTurnedDownIsNotBoughtAgain:
    """The check reads the name and the question, and neither changes between arrivals.
    신용협동조합 was proposed and turned down eight times under one question in a single
    run, 상생협력 nine."""

    async def test_the_same_name_under_the_same_question_is_refused_from_memory(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 8)
        _emerges(script, "문학", "문학 자료", ids[:2])
        script.set_name_is_beside()
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        assert not [p for p in llm.prompts_for(None) if "THE PROPOSED NAME: " in p.user]


class TestANameAnswersTheFoldersQuestion:
    """The property is checked when it is chosen and never again, so nothing read the
    names it was supposed to produce. A collection divided on 적용 대상 -- who the law
    applies to -- took 중대재해처벌법, 디지털포용법 and 테러자금금지법 as answers, and
    eight of the root's 25 folders ended up named after a statute."""

    async def test_a_name_that_answers_something_else_builds_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 6)
        _emerges(script, "중대재해처벌법", "중대재해 관련 문서", ids[:2], axis="적용 대상")
        script.set_name_is_beside()

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert not (Path(engine.vault.root) / "중대재해처벌법").exists()

    async def test_it_is_asked_before_the_membership_loop(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """One closed question against one call per document standing here."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "중대재해처벌법", "중대재해 관련 문서", ids[:2], axis="적용 대상")
        script.set_name_is_beside()

        await engine.subdivision.consider(PurePosixPath())

        assert not [p for p in llm.prompts_for(None) if "NEW SIGN:" in p.user]

    def test_the_shared_sentence_has_a_ceiling(self) -> None:
        """One reply ran the same handful of nouns to the 8,192-token generation limit,
        three attempts running, and the whole call was thrown away."""
        with pytest.raises(ValidationError):
            subdivision_prompts.Gathered(members=["D0001", "D0002"], shared="금융회사등, " * 60)


class TestTheQuestionCoversTheFolder:
    """The axis was chosen from the group that prompted it and nothing else. A collection
    of 300 laws was divided on 어떤 분야의 상생협력을 촉진하는가 -- exactly right for the
    four documents that suggested it, unanswerable for the other 290 -- and every honest
    name after it was refused, correctly and uselessly, 103 times."""

    async def test_the_rest_of_the_folder_is_in_the_question(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        asked = llm.prompts_for(subdivision_prompts.Axis)
        assert asked
        assert "WHAT THE REST OF THE FOLDER IS ABOUT:" in asked[-1].user
        # Topics, not titles: shown titles, this step read the kind of instrument off them.
        assert "아폴로" in asked[-1].user

    async def test_a_folder_with_nothing_left_over_is_not_told_about_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """An empty list is a line that says nothing, so it is not sent."""
        prompt = subdivision_prompts.build_axis(shared="같은 주제", rest=[])

        assert "WHAT THE REST OF THE FOLDER IS ABOUT:" not in prompt.user
