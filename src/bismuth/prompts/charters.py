"""Writing a folder's routing sign: one line describing what belongs there."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from bismuth.ports.llm import Prompt

SYSTEM = """\
You are writing the one-line note for a folder in a shared archive. It is read by \
two things: an automatic filer deciding whether the NEXT document belongs here, \
and a person (or agent) browsing the tree.

Write in the archive's own language -- the same language as the documents shown.

Return only `purpose`: one short positive routing rule saying what belongs here. \
It must be concrete enough that a future document can be accepted or ruled out.

Some folders mostly organise SUBFOLDERS rather than hold documents directly. When \
subfolders are listed, describe the shared boundary that makes them children here, \
so the note still tells the filer whether a new document belongs in this subtree.

Describe the folder's ROLE, not its current inventory. Do not mention counts, sample \
documents, excluded material, classification steps, uncertainty, or how the folder was made.\
"""

_USER = """\
FOLDER: {path}

DIRECT DOCUMENTS: {count}{sample_note}
{documents}

CHILD FOLDERS:
{children}\
"""


class CharterDraft(BaseModel):
    purpose: str = Field(description="One short positive routing rule: what belongs here.")

    @field_validator("purpose")
    @classmethod
    def _one_line(cls, value: str) -> str:
        normalised = " ".join(value.split()).strip()
        if not normalised:
            raise ValueError("purpose must not be empty")
        return normalised


def build(
    *,
    path: str,
    document_briefs: list[str],
    total_count: int,
    children: list[tuple[str, str]] | None = None,
) -> Prompt:
    children = children or []
    child_lines = (
        "\n".join(f"  {name}" + (f"  — {purpose}" if purpose else "") for name, purpose in children)
        or "(none)"
    )
    return Prompt(
        system=SYSTEM,
        user=_USER.format(
            path=path or "/",
            count=total_count,
            sample_note=" (representative subset shown)"
            if len(document_briefs) < total_count
            else "",
            documents="\n".join(document_briefs)
            or "(none; infer the shared boundary from child folders and the path)",
            children=child_lines,
        ),
    )
