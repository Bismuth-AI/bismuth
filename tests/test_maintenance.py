from pathlib import PurePosixPath

from bismuth.domain.charter import Charter
from bismuth.domain.maintenance import (
    PlanProblem,
    ProposedClass,
    is_axis_label,
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
        available_document_ids=frozenset({"a", "b", "c"}),
    )

    assert result.accepted


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
