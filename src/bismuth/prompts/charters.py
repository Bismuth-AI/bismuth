"""Writing a folder's note: one line describing what it holds, for placement to file against."""

from __future__ import annotations

from pydantic import BaseModel, Field

from bismuth.ports.llm import Prompt

SYSTEM = """\
You are writing the one-line note for a folder in a shared archive. It is read by \
two things: an automatic filer deciding whether the NEXT document belongs here, \
and a person (or agent) browsing the tree.

Write in the archive's own language -- the same language as the documents shown.

`purpose`: one line. What this folder holds and what belongs in it. Concrete \
enough to file against: "아폴로 사업의 계약서·보고서·회의록" is useful; "여러 문서" \
is not.

`holds`: two or three concrete examples of what belongs here, drawn from the \
documents shown but stated as a rule for the future, not a stocktake.

`answers`: two or three real questions whose answers live in this folder, phrased \
as a colleague would ask them.

Some folders mostly organise SUBFOLDERS rather than hold documents directly. When \
subfolders are listed, describe the folder's role as the parent of them -- e.g. \
"아폴로 사업 문서를 연도별 하위 폴더로 나눠 보관" -- so the note still tells the filer \
whether a new document belongs somewhere in this subtree.

Describe the folder's ROLE, not its current inventory -- a note that lists today's \
files is wrong tomorrow.\
"""

_USER = """\
FOLDER: {path}

문서 {count}개가 이 폴더에 직접 들어있습니다{sample_note}:
{documents}

하위 폴더:
{children}\
"""


class CharterDraft(BaseModel):
    title: str = Field(description="Short human name for the folder, in the archive's language.")
    purpose: str = Field(description="One line: what it holds.")
    holds: list[str] = Field(default_factory=list, max_length=4)
    answers: list[str] = Field(default_factory=list, max_length=4)


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
        or "(없음)"
    )
    return Prompt(
        system=SYSTEM,
        user=_USER.format(
            path=path or "/ (아카이브 루트)",
            count=total_count,
            sample_note=" (일부만 표시)" if len(document_briefs) < total_count else "",
            documents="\n".join(document_briefs)
            or "(이 폴더에 직접 있는 문서 없음 -- 하위 폴더나 경로로 역할을 추정하세요)",
            children=child_lines,
        ),
    )
