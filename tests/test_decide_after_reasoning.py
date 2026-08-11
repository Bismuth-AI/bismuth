"""Decision schemas expose only fields that the application consumes.

Constrained decoding fills fields in declaration order. Measured on 300 legal
documents, putting ``Emerging.emerged`` first produced 300 false verdicts even
when the same response later supplied a coherent candidate. The candidate must
therefore be formed before the verdict, without adding a free-form scratchpad.
"""

from bismuth.prompts import subdivision as subdivision_prompts


def test_emerging_forms_the_candidate_before_the_verdict() -> None:
    fields = list(subdivision_prompts.Emerging.model_json_schema()["properties"])

    assert fields == ["axis", "axis_question", "name", "note", "emerged"]


def test_review_contains_only_checks_that_gate_the_decision() -> None:
    fields = list(subdivision_prompts.Review.model_json_schema()["properties"])

    assert fields == ["one_axis", "coherent_membership", "useful_navigation"]


def test_maintenance_schemas_have_no_free_form_reason_metadata() -> None:
    schemas = (
        subdivision_prompts.Emerging,
        subdivision_prompts.Members,
        subdivision_prompts.Division,
        subdivision_prompts.Review,
        subdivision_prompts.Replacement,
        subdivision_prompts.BoundaryAudit,
        subdivision_prompts.ReplacementAudit,
        subdivision_prompts.ExistingAssignments,
        subdivision_prompts.RoutingAudit,
    )

    assert all("reason" not in schema.model_fields for schema in schemas)
