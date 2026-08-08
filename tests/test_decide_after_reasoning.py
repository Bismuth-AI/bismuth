"""Every decision schema states its reasoning before its verdict.

Constrained decoding fills fields in schema order, so the first field is answered with
nothing behind it. Measured on 300 legal documents through a schema-constrained endpoint:
the folder question was asked 300 times and answered `emerged: false` 300 times -- while
the same replies went on to name the axis (`법령의 종류`), the class (`법률`), and explain
at length that it covered half the archive. The model had the right answer and had
already committed to the wrong one.

Nothing enforces this but the order the fields are declared in, which is exactly the kind
of thing that gets tidied away by someone grouping the required fields together.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import subdivision as subdivision_prompts

# (schema, the field that commits to something)
DECISIONS: list[tuple[type[BaseModel], str]] = [
    (subdivision_prompts.Emerging, "emerged"),
    (subdivision_prompts.Members, "document_ids"),
    (subdivision_prompts.Division, "divide"),
    (subdivision_prompts.Review, "holds"),
    (placement_prompts.PlacementDecision, "folder"),
]


@pytest.mark.parametrize(
    ("schema", "verdict"), DECISIONS, ids=lambda v: v.__name__ if hasattr(v, "__name__") else v
)
def test_the_reason_is_written_before_the_verdict(schema: type[BaseModel], verdict: str) -> None:
    fields = list(schema.model_json_schema()["properties"])

    assert fields[0] == "reason", f"{schema.__name__} answers before it thinks"
    assert fields.index("reason") < fields.index(verdict)


def test_the_reason_asks_for_reasoning_not_a_summary() -> None:
    """A field described as "one sentence about what you decided" invites the model to
    justify a choice it has already made, which is the same failure worded politely."""
    described = subdivision_prompts.Emerging.model_fields["reason"].description or ""
    assert "before" in described.lower()
