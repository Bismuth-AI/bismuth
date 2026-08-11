"""Choose one step through the folder tree that already exists.

The model never writes a path.  At each level it sees only the direct child signs,
addressed by compact deterministic handles, and either chooses one or stays where it
is. This prevents spelling, slash and invented-path failures and makes "none fits"
local: choosing a parent never forces a document into an unsuitable child below it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from bismuth.ports.llm import Prompt

SYSTEM = """\
You are a meticulous library clerk choosing ONE next step through an existing folder \
tree. You are shown the current folder, the document, and only the direct child signs.

Return exactly one child ID shown, or `""` to KEEP THE DOCUMENT IN THE CURRENT FOLDER. \
Return null only when the document itself is unreadable or garbage.

Choose a child only when its sign positively describes the document. Never choose the \
closest child merely because no better child exists. Having one child does not make it \
the default. Staying here is a confident, normal filing decision and lets a later \
maintenance pass create a real class after several related documents gather.

The IDs are opaque handles. Do not copy, translate, repair or compose folder names. \
There is no field for an explanation because the application uses only the decision.
"""

_USER = """\
CURRENT FOLDER: {current}
DIRECT CHILD SIGNS:
{children}

DOCUMENT TO FILE
title: {title}
type: {doc_type}
about: {topics}
summary: {summary}
mentions: {entities}\
"""


class PlacementDecision(BaseModel):
    """One direct-child choice; an empty ID means stay at the current level."""

    folder_id: str | None = Field(
        description=(
            "One shown direct-child ID; '' to stay in the current folder; null only "
            "when the document could not be read."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("folder_id")
    @classmethod
    def _normalise_folder_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = value.strip().rstrip("/\\").strip().strip("[]").strip().upper()
        # A single dot conventionally means the current directory. It is not an opaque
        # handle, but its safe intent is unambiguous: stay where we are.
        return "" if normalised == "." else normalised


def build(
    *,
    current: str,
    children: list[tuple[str, str, str]],
    title: str,
    doc_type: str,
    topics: list[str],
    summary: str,
    entities: list[str],
) -> Prompt:
    """Build one level of placement.

    ``children`` contains ``(stable handle, direct child name, purpose)`` tuples.
    """
    rendered = (
        "\n".join(
            f"  [{folder_id}] {name}" + (f" — {purpose}" if purpose else "")
            for folder_id, name, purpose in children
        )
        if children
        else "  (none — keep the document here)"
    )
    return Prompt(
        system=SYSTEM,
        user=_USER.format(
            current=current or "(root)",
            children=rendered,
            title=title,
            doc_type=doc_type,
            topics=", ".join(topics) or "(none)",
            summary=summary,
            entities=", ".join(entities) or "(none)",
        ),
        cache_hint=True,
    )
