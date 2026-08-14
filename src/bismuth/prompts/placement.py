"""Choose one step through the folder tree that already exists.

The model never writes a path.  At each level it sees only the direct child signs,
addressed by compact deterministic handles, and either chooses one or stays where it
is. This prevents spelling, slash and invented-path failures and makes "none fits"
local: choosing a parent never forces a document into an unsuitable child below it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from bismuth.ports.llm import Prompt

SYSTEM = """\
You are a meticulous library clerk choosing ONE next step through an existing folder \
tree. You are shown the current folder, the document, and only the direct child signs.

Return exactly one child ID shown, `STAY` to KEEP THE DOCUMENT IN THE CURRENT FOLDER, \
or `UNREADABLE` only when the document itself is unreadable or garbage.

Choose a child only when its sign positively describes the document. Never choose the \
closest child merely because no better child exists. Having one child does not make it \
the default. Staying here is a confident, normal filing decision and lets a later \
maintenance pass create a real class after several related documents gather.
Keep editions, revisions, and subordinate instruments with their named document family. \
Do not descend into a narrower child unless its sign describes the whole document rather \
than one incidental topic, agency, penalty, or section mentioned in the summary.

The IDs are opaque handles. Do not copy, translate, repair or compose folder names. \
There is no JSON and no explanation because the application uses only the one literal.
"""

AGENT_SYSTEM = """\
You are the placement half of an AI librarian. File exactly ONE readable document into
the library as it exists now. The folder list contains stable opaque IDs, paths and routing
signs. Prefer a positively fitting existing folder. Keeping the document at the root or an
existing parent is a normal answer when every child is too narrow.

You may inspect the few plausible folders needed to compare them. Each folder inspection
already includes representative documents and can be read once; stop as soon as the
evidence decides the placement. Do not inventory unrelated shelves or redesign the vault.
You are not allowed to create, rename, split, or repair folders. One document cannot supply
the contrastive evidence needed to choose a reusable classification boundary. If no shown
folder positively fits, keep the document at the root or the narrowest fitting existing
parent. A separate structure harness examines multiple colocated documents after filing.

An organization name containing a subject word does not make its internal-administration
documents members of that subject shelf. If the document's primary subject is the
organization's own structure, staffing, offices, jurisdiction, establishment, or supervision,
keep it outside the policy/industry domain the organization administers. Judge what the
document regulates, not the words inside the authority's name.

Finish by calling `finish_placement` exactly once. Do not return a prose answer. The host
validates every folder handle; tools never mutate the filesystem.
"""

_AGENT_USER = """\
DOCUMENT
title: {title}
type: {doc_type}
about: {topics}
summary: {summary}
mentions: {entities}

CURRENT FOLDER SIGNS
{folders}

POSSIBLY RELATED DOCUMENTS
{related}

Choose the best current shelf. Inspect only evidence that can change this one placement.\
"""

class InspectFolderArgs(BaseModel):
    folder_id: str = Field(description="One F-prefixed folder ID shown in the task.")


class InspectDocumentArgs(BaseModel):
    """Legacy schema retained for compatibility; production no longer exposes this tool."""

    document_id: str = Field(description="One D-prefixed related-document ID shown in the task.")


class FinishPlacementArgs(BaseModel):
    action: Literal["place_existing", "keep_here"]
    folder_id: str = Field(
        default="FROOT",
        description="Existing destination. FROOT is the vault root.",
    )


def build_agent(
    *,
    title: str,
    doc_type: str,
    topics: list[str],
    summary: str,
    entities: list[str],
    folders: list[tuple[str, str, str]],
    related: list[tuple[str, str, str, str]],
) -> str:
    """Build the bounded evidence task for one tool-using placement run."""

    folder_lines = [
        f"[{folder_id}] {path or '/'} — {purpose or '(no routing sign)'}"
        for folder_id, path, purpose in folders
    ]
    related_lines = [
        f"[{document_id}] AT={path or '/'} | {title} | {topics}"
        for document_id, path, title, topics in related
    ]
    return _AGENT_USER.format(
        title=title,
        doc_type=doc_type,
        topics=", ".join(topics) or "(none)",
        summary=summary,
        entities=", ".join(entities) or "(none)",
        folders="\n".join(folder_lines) or "[FROOT] / — vault root",
        related="\n".join(related_lines) or "(none)",
    )


def build_fit_audit(
    *,
    title: str,
    doc_type: str,
    topics: list[str],
    summary: str,
    path: str,
    sign: str,
    examples: list[tuple[str, str]] | None = None,
    alternatives: list[tuple[str, str]] | None = None,
) -> Prompt:
    """Closed final check that an explored destination positively fits the document."""
    return Prompt(
        system=(
            "Verify one proposed existing-shelf placement. Reply SHELF only when the "
            "document's PRIMARY SUBJECT is an ordinary member of the reusable category named "
            "by the shelf sign, AT THE SIGN'S OWN LEVEL OF ABSTRACTION. Broad domain signs are "
            "expected to contain different subtopics. Representative members are positive "
            "examples that reveal the boundary; they are not an exhaustive prototype and the "
            "document need not share their narrow subtopic. A new regulated population, legal "
            "mechanism, or industry can be an ordinary DIFFERENT SUBTOPIC of a broad subject "
            "sign; do not require it to be synonymous with a representative. Test the topical "
            "parent-child relation to the sign itself. Mere relation, a shared authority, "
            "incidental sections, or shared words are still not enough. A document whose "
            "primary subject is an organization's own structure, staffing, offices, jurisdiction, "
            "establishment, or supervision is public/institutional administration, not an "
            "ordinary member of the policy or industry domain contained in that organization's "
            "name. Reply STAY in that case. Reply STAY when the "
            "document's primary subject falls outside the sign or a peer sibling "
            "category would describe it more honestly, even if that sibling does not exist "
            "yet. Reply exactly SHELF or STAY."
        ),
        user=(
            f"PROPOSED SHELF: {path}/ — {sign}\n\n"
            "REPRESENTATIVE MEMBERS:\n"
            + (
                "\n".join(f"- {member_title} | {member_topics}" for member_title, member_topics in (examples or []))
                or "(none)"
            )
            + "\n\nOTHER EXISTING SUBJECT SIGNS:\n"
            + (
                "\n".join(f"- {other_path}/ — {other_sign}" for other_path, other_sign in (alternatives or []))
                or "(none)"
            )
            + "\n\n"
            f"DOCUMENT\ntitle: {title}\ntype: {doc_type}\n"
            f"about: {', '.join(topics) or '(none)'}\nsummary: {summary}"
        ),
    )

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
    """Legacy FakeLLM fixture; production Placement uses the plain-choice LLM port.

    Kept temporarily so downstream test scripts written against the former structured
    response can continue to drive FakeLLM while the on-wire contract is plain text.
    """

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
