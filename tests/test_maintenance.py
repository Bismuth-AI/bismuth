from pathlib import PurePosixPath

from bismuth.domain.charter import Charter
from bismuth.domain.maintenance import (
    PlanProblem,
    ProposedClass,
    is_axis_label,
    restates,
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


def test_an_older_boundary_requires_the_complete_review_contract() -> None:
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

    assert charter.boundary_review_required
    assert charter.due_for_review(10)


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
