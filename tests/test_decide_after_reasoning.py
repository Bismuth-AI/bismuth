"""Decision schemas expose only fields that the application consumes.

Constrained decoding fills fields in declaration order. Measured on 300 legal
documents, putting ``Emerging.emerged`` first produced 300 false verdicts even
when the same response later supplied a coherent candidate. The candidate must
therefore be formed before the verdict, without adding a free-form scratchpad.
"""

from bismuth.prompts import subdivision as subdivision_prompts


def test_emerging_forms_the_candidate_before_the_verdict() -> None:
    fields = list(subdivision_prompts.Emerging.model_json_schema()["properties"])

    assert fields == ["sign", "name", "axis", "axis_question", "emerged"]


def test_maintenance_schemas_have_no_free_form_reason_metadata() -> None:
    schemas = (
        subdivision_prompts.Emerging,
        subdivision_prompts.Members,
        subdivision_prompts.Division,
        subdivision_prompts.Gathered,
        subdivision_prompts.ClassName,
        subdivision_prompts.ClassSign,
        subdivision_prompts.Axis,
        subdivision_prompts.Grouping,
        subdivision_prompts.ExistingAssignments,
    )

    assert all("reason" not in schema.model_fields for schema in schemas)
