from pathlib import PurePosixPath

from bismuth.domain.charter import Charter
from bismuth.domain.maintenance import (
    FolderShape,
    GroupingProblem,
    Operator,
    PlanProblem,
    ProposedClass,
    is_axis_label,
    legal_operators,
    restates,
    validate_grouping,
    validate_plan,
)


def test_axis_syntax_is_language_neutral() -> None:
    assert is_axis_label("분류 속성")
    assert is_axis_label("Eigenschaft der Sammlung")
    assert is_axis_label("The distinction was made when there were")
    assert not is_axis_label("")
    assert not is_axis_label("two\nlines")


def test_one_document_cannot_get_a_sign() -> None:
    result = validate_plan(
        axis="주제 분야",
        axis_question="어느 분야에 속하는가?",
        groups=(ProposedClass("문학", ("a",)),),
        available_document_ids=frozenset({"a", "b"}),
    )

    assert PlanProblem.SINGLE_DOCUMENT in result.problems


def test_a_document_cannot_be_assigned_to_two_classes() -> None:
    result = validate_plan(
        axis="주제 분야",
        axis_question="어느 분야에 속하는가?",
        groups=(
            ProposedClass("문학", ("a", "b")),
            ProposedClass("과학", ("b", "c")),
        ),
        available_document_ids=frozenset({"a", "b", "c", "d"}),
    )

    assert PlanProblem.DUPLICATE_DOCUMENT in result.problems


def test_a_document_repeated_inside_one_class_is_also_rejected() -> None:
    result = validate_plan(
        axis="주제 분야",
        axis_question="어느 분야에 속하는가?",
        groups=(ProposedClass("문학", ("a", "a", "b")),),
        available_document_ids=frozenset({"a", "b", "c"}),
    )

    assert PlanProblem.DUPLICATE_DOCUMENT in result.problems


def test_a_valid_partial_class_is_safe_to_apply() -> None:
    result = validate_plan(
        axis="주제 분야",
        axis_question="어느 분야에 속하는가?",
        groups=(ProposedClass("문학", ("a", "b")),),
        available_document_ids=frozenset({"a", "b", "c", "d"}),
    )

    assert result.accepted


def test_a_class_that_leaves_one_document_behind_is_a_rename() -> None:
    result = validate_plan(
        axis="주제 분야",
        axis_question="어느 분야에 속하는가?",
        groups=(ProposedClass("문학", ("a", "b")),),
        available_document_ids=frozenset({"a", "b", "c"}),
    )

    assert PlanProblem.NO_DIVISION in result.problems


def test_an_axis_label_cannot_be_used_as_its_own_class_name() -> None:
    result = validate_plan(
        axis="규정 대상 및 내용",
        axis_question="무엇을 규정하는가?",
        groups=(ProposedClass("규정 대상 및 내용", ("a", "b")),),
        available_document_ids=frozenset({"a", "b", "c"}),
    )

    assert PlanProblem.AXIS_AS_NAME in result.problems


def test_a_complete_review_cannot_leave_documents_behind() -> None:
    result = validate_plan(
        axis="collection property",
        axis_question="Which value of the collection property applies?",
        groups=(ProposedClass("value a", ("a", "b")),),
        available_document_ids=frozenset({"a", "b", "c"}),
        require_complete=True,
    )

    assert PlanProblem.UNASSIGNED_DOCUMENT in result.problems


def test_an_older_note_keeps_the_boundary_it_recorded() -> None:
    """Nothing re-audits it on the way in any more: the whole-collection pass is what
    looks at an old boundary, and it looks at every folder rather than at whichever one
    happened to be written by an older Bismuth."""
    old_note = """---
bismuth_charter: 3
managed: true
title: archive
purpose: collection
holds: []
answers: []
split_basis: old property
split_question: Which old property value applies?
split_at_documents: 10
---
"""

    charter = Charter.from_markdown(old_note, path=PurePosixPath())

    assert charter.divided
    assert charter.split_basis == "old property"
    assert charter.split_at_documents == 10


class TestAChildMayNotRestateItsAncestor:
    """Both directions of containment, for different reasons."""

    def test_a_name_inside_an_ancestors_name_repeats_a_settled_distinction(self) -> None:
        # 대통령령 총리령(하위시행규정)/…/대통령령 -- the grandchild names one half of a
        # compound its ancestor had already resolved.
        assert restates("대통령령", "대통령령 총리령(하위시행규정)")

    def test_a_name_that_only_decorates_an_ancestors_phrase_sorts_nothing(self) -> None:
        """Every document in the parent answers to such a name; that is what made it the
        parent's name. Observed twice after the prompt was told not to: 연구인프라 및 인력
        지원 → 국가연구인프라 및 인력 지원, and 기업 유형별 지원 → 기업 유형별 지원 법령,
        which held two documents above thirty-eight."""
        assert restates("기업 유형별 지원 법령", "기업 유형별 지원")
        assert restates("국가연구인프라 및 인력 지원", "연구인프라 및 인력 지원")

    def test_a_one_word_ancestor_is_refined_by_adding_words(self) -> None:
        """Refusing this direction outright would forbid most real trees."""
        assert not restates("금융소비자보호", "금융")
        assert not restates("중소기업 기술혁신", "중소기업")
        assert not restates("금융감독 및 건전성 규제", "금융감독")

    def test_different_words_are_not_a_restatement(self) -> None:
        assert not restates("나노기술", "과학기술")
        assert not restates("금융", "금융")


class TestAShelfMayNotBeBuiltOverItsOwnName:
    """The name contract, applied when a folder is moved under a shelf and not only when
    it is created.

    Checked at creation only, a shelf could be stood over a folder that restates it:
    위반 행위 및 제재 was built over 위반 행위 및 제재 유형, a pair restates() refuses
    outright between a parent and a child. The corridor of near-synonyms it started
    reached six levels -- past the absolute ceiling -- and left a folder holding no
    documents in the middle of it.
    """

    def test_a_member_that_restates_the_shelf_is_refused(self) -> None:
        result = validate_grouping(
            name="위반 행위 및 제재",
            axis="규제 대상 행위",
            members=("위반 행위 및 제재 유형", "과태료 부과 사유"),
            siblings=("위반 행위 및 제재 유형", "과태료 부과 사유", "채무조정", "경영정상화"),
            ancestor_names=("금융기관 또는 금융회사등", "금융 규제 및 감독 법령"),
        )

        assert GroupingProblem.MEMBER_RESTATES_NAME in result.problems

    def test_folders_that_answer_the_shelf_still_stand_under_it(self) -> None:
        """The contract refuses repetition, not grouping."""
        result = validate_grouping(
            name="금융 감독",
            axis="규제 대상 행위",
            members=("보험사기행위", "유사수신행위"),
            siblings=("보험사기행위", "유사수신행위", "채무조정"),
            ancestor_names=("금융기관 또는 금융회사등",),
        )

        assert result.accepted


class TestWhatMayBeAsked:
    """ADR-0018: code enumerates what is possible here, the model picks one."""

    def test_keep_is_always_offered(self) -> None:
        """Without it the reply would be about whether to act, not about what to do."""
        assert legal_operators(FolderShape(loose_documents=0, is_root=True)) == frozenset(
            {Operator.KEEP}
        )

    def test_a_pile_too_small_to_divide_is_not_asked_to_divide(self) -> None:
        """Two documents make a class and leave no remainder, so the plan is refused
        either way -- NO_DIVISION. Asking is what used to be paid for."""
        assert Operator.CREATE not in legal_operators(FolderShape(loose_documents=3, is_root=True))

    def test_a_pile_that_can_lose_a_class_and_still_be_a_pile_is_asked(self) -> None:
        assert Operator.CREATE in legal_operators(FolderShape(loose_documents=4, is_root=True))

    def test_two_folders_cannot_be_grouped(self) -> None:
        """Both would move, which renames this folder rather than tidying it."""
        shape = FolderShape(loose_documents=0, children=("문학", "역사"), is_root=True)

        assert Operator.MERGE not in legal_operators(shape)

    def test_three_folders_can(self) -> None:
        shape = FolderShape(loose_documents=0, children=("문학", "역사", "과학"), is_root=True)

        assert Operator.MERGE in legal_operators(shape)

    def test_the_root_is_never_dissolved(self) -> None:
        """It has nowhere to promote to."""
        shape = FolderShape(loose_documents=9, children=("문학",), is_root=True)

        assert Operator.SPLIT not in legal_operators(shape)

    def test_a_level_holding_something_can_be_dissolved(self) -> None:
        shape = FolderShape(
            loose_documents=2,
            children=("현상학",),
            ancestor_names=("인문",),
            siblings=("역사",),
        )

        assert Operator.SPLIT in legal_operators(shape)

    def test_a_level_whose_children_collide_upstairs_cannot_be(self) -> None:
        """The promotion would land beside a folder of the same name, so it is not
        offered rather than proposed and refused."""
        shape = FolderShape(
            loose_documents=0,
            children=("현상학",),
            ancestor_names=("인문",),
            siblings=("현상학", "역사"),
        )

        assert Operator.SPLIT not in legal_operators(shape)

    def test_the_reverse_of_the_last_operator_waits_for_new_evidence(self) -> None:
        """Merge and split undo each other, so on unchanged evidence they would undo each
        other for ever. The reverse is not enumerated rather than watched for."""
        shape = FolderShape(
            loose_documents=0,
            children=("문학", "역사", "과학"),
            ancestor_names=("인문",),
            siblings=("사회",),
            last_operator=Operator.SPLIT,
            evidence_moved=False,
        )

        assert Operator.MERGE not in legal_operators(shape)
        assert Operator.SPLIT in legal_operators(shape)

    def test_once_the_evidence_moves_it_is_offered_again(self) -> None:
        shape = FolderShape(
            loose_documents=0,
            children=("문학", "역사", "과학"),
            ancestor_names=("인문",),
            siblings=("사회",),
            last_operator=Operator.SPLIT,
            evidence_moved=True,
        )

        assert Operator.MERGE in legal_operators(shape)


class TestMovingUnderAFolderThatStandsHere:
    """The other half of merge: the shelf is already built (ADR-0018)."""

    def test_a_taken_name_is_the_point_not_a_collision(self) -> None:
        result = validate_grouping(
            name="금융",
            axis="주제",
            members=("가상자산", "벤처투자", "신용정보"),
            siblings=("금융", "가상자산", "벤처투자", "신용정보", "과학기술", "소비자"),
            into_existing=True,
        )

        assert result.accepted

    def test_the_same_proposal_is_refused_when_a_new_shelf_is_meant(self) -> None:
        result = validate_grouping(
            name="금융",
            axis="주제",
            members=("가상자산", "벤처투자", "신용정보"),
            siblings=("금융", "가상자산", "벤처투자", "신용정보", "과학기술", "소비자"),
        )

        assert GroupingProblem.NAME_EXISTS in result.problems

    def test_a_shelf_that_is_not_standing_here_is_refused(self) -> None:
        result = validate_grouping(
            name="금융",
            axis="주제",
            members=("가상자산", "벤처투자"),
            siblings=("가상자산", "벤처투자", "소비자"),
            into_existing=True,
        )

        assert GroupingProblem.SHELF_IS_NOT_HERE in result.problems

    def test_a_folder_cannot_stand_inside_itself(self) -> None:
        result = validate_grouping(
            name="금융",
            axis="주제",
            members=("금융", "가상자산"),
            siblings=("금융", "가상자산", "소비자"),
            into_existing=True,
        )

        assert GroupingProblem.NAME_IS_A_MEMBER in result.problems

    def test_the_shelf_counts_as_one_of_the_folders_left_standing(self) -> None:
        """Moving everything else inside it leaves this folder with one child."""
        result = validate_grouping(
            name="금융",
            axis="주제",
            members=("가상자산", "벤처투자"),
            siblings=("금융", "가상자산", "벤처투자"),
            into_existing=True,
        )

        assert GroupingProblem.TOOK_EVERY_FOLDER in result.problems
