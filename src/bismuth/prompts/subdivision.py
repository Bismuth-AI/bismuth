"""Asking what has grown here, and who belongs to it.

The shape of each question is the point, and the shape is not "divide this".

**Normal growth is not a partition.** A partition has to account for every document, and
a heterogeneous pile cannot honestly be accounted for without inventing a remainder
class. Forbidding remainder names does not work, because the partition demands one.

So a first boundary proposes at least two sibling signs together, while leaving unrelated
documents at the parent. Once that axis exists, *Emerging* names one additional answer and
*Members* says who belongs to it. Neither flow needs a remainder class; leftovers stay in
the folder they are already in, which is what SPEC.md 3.4 says should happen to them.

Asking this repeatedly is safe in a way that asking "how would you divide this" is not:
it can add one sibling or route a loose document behind an existing sign, but it cannot
change the axis or redraw existing siblings.

*Still right?* is what an already-divided folder is asked once the evidence has doubled,
and it is the only question that may redraw a boundary.

There is no free-form reasoning metadata in these schemas. Emerging writes its concrete
candidate before its verdict so constrained decoding does not commit before identifying
what allegedly emerged. Review returns only checks that directly decide whether the
boundary holds. After a failed review, membership-free replacement signs are designed
first and request-local document handles are assigned in separate bounded packets.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from bismuth.domain.maintenance import is_axis_label
from bismuth.ports.llm import Prompt

_SIGNS = """\
Think of folders as SIGNS, not as groups.

Someone is looking for a document. Right now they see the list of documents in this \
folder. If it is divided they see a few folder names INSTEAD, and must choose one \
before they see any document at all. A division is worth making when those names let \
them ignore most of the collection, and it is worth nothing otherwise.

A division fails when:

- **There are nearly as many signs as documents.** Reading a second list merely to \
reach the first list adds navigation without narrowing it.
- **A sign points at one document.** A sign in front of a single book is not a sign, \
it is the book with a step in front of it.
- **Most documents fit no sign.** Then the reader is back to scanning, and the \
distinction drawn is not the one this collection is organised by.

A sign names a CLASS -- something you expect more of -- never one document's subject. \
If the only honest name for a group is the title of the document inside it, that group \
should not exist.

Signs and notes in the DOCUMENTS' OWN LANGUAGE.\
"""

_EMERGING_SYSTEM = f"""\
These documents have not been sorted, and this folder has no sub-folders yet. You are \
deciding TWO things, and the first one outlives this answer.

**First, the AXIS.** This first boundary is a subject catalogue: the property is the \
documents' primary subject domain, and every sub-folder must be one specific answer to \
"What primary subject domain is this document about?". Organisations, publishers, \
responsible authorities, document/form types, dates, languages, and source field labels \
are evidence or metadata, never the axis and never a folder name. Do not copy labels such \
as SUBJECT_TOPICS, KEYWORDS, SUBJECT_DOMAIN, or ORGANIZATIONS. Return a short axis label \
for primary subject domain and that one question. Derive the concrete domain answers only \
from recurring document evidence; no preferred taxonomy or example domain is supplied.

**Then, the first CLASS on it** -- the one value of that axis that has gathered enough \
documents to be worth a shelf.

{_SIGNS}

**You are not dividing this folder and you are not accounting for every document.** \
Whatever does not belong to the class you name stays exactly where it is. Most of these \
documents staying put is the normal outcome, not a failure.

If two classes have grown, name the thicker; you will be asked again and the other can \
come out then.

`emerged` is false when nothing has gathered yet -- common in a young archive, and the \
right answer more often than not. A collection whose documents are each about something \
different is a list, and a list is best left as a list. Say so.\
"""

_EMERGING_ALONG_SYSTEM = f"""\
This folder is already divided, and it is divided ALONG ONE AXIS. You are told what that \
axis is and which answers to it already have shelves. You are deciding one thing: \
whether ANOTHER answer to that same question has gathered enough documents here.

**The axis is not yours to change.** A class on a different axis would put two kinds of \
distinction side by side, and then no name here rules anything out -- a reader has to \
open all of them, which is the cost the folders exist to avoid. If what has gathered is \
real but belongs to a different question, the answer is no: it stays in this folder, and \
a later look at a different level can take it.

{_SIGNS}

Whatever you do not name stays exactly where it is, and most of it staying is normal.

An existing shelf is not an emerging class. Never return, paraphrase, broaden, or narrow
an answer already listed under CHILDREN. Search the documents still sitting directly in
this folder for a DIFFERENT recurring answer. If none exists, return emerged=false and an
empty name; do not echo the closest existing sign.

`emerged` is false when nothing new has gathered along this axis. That is the common \
answer and the safe one. Leave `axis` empty; this folder already has one.\
"""

_MEMBERS_SYSTEM = f"""\
A shelf has been decided on and named. Say which of these documents go on it.

{_SIGNS}

Only the ones that genuinely belong under this sign. Everything else stays in the folder \
it is in now -- you are not placing those and you are not being asked about them. \
Leaving most of the list unclaimed is normal and expected.

A document belongs here when someone looking for it would read this sign and stop. If \
you find yourself reasoning that it fits better here than anywhere else, it does not \
belong: there is no anywhere else, there is only this shelf and the folder it is \
already in. An organization name containing the sign is not membership: a document whose \
primary subject is that organization's own structure, staffing, offices, jurisdiction, \
establishment, or supervision stays outside the policy/industry subject it administers.\
"""

_REVIEW_SYSTEM = f"""\
You are re-examining a folder you divided earlier. You are told what distinction it \
was divided along and how many documents there were at the time. There are more now. \
This request is one isolated evidence packet. Across packets the application presents \
every document and every direct sign, then combines the checks fail-closed. Judge only \
whether this packet supplies evidence that the current boundary fails; do not assume \
that documents or signs absent from this packet do not exist.

**Your default answer is that the existing division still holds.** It was made by \
someone looking at the same archive, and moving documents has a cost paid by every \
person who had learned where things were. Overturn it only if you can say what is \
actually wrong -- a sign that no longer means anything, a distinction that has turned \
out to cut across the real one, documents that have gathered and clearly belong \
together elsewhere.

{_SIGNS}

Judge the current boundary only. Do not propose a replacement, enumerate documents, \
recount memberships, or explain your work. Each output boolean is a directly used check; \
the application derives whether the boundary holds from all of them. If no current axis \
question was recorded, `one_axis` is false because the sibling contract is incomplete.\
"""

_LISTING = """\
FOLDER: {path}
{purpose}
DOCUMENTS SITTING DIRECTLY HERE ({count}):
{documents}
{children}\
"""

_REVIEW_USER = """\
FOLDER: {path}
{purpose}
DIVIDED ALONG: {basis}
CURRENT AXIS QUESTION: {basis_question}
AT THE TIME THERE WERE {before} DOCUMENTS; THERE ARE NOW {count}.

CURRENT SUBTREE FOLDERS:
{children}

DOCUMENT EVIDENCE IN THIS ISOLATED PACKET ({loose}):
{documents}\
"""


class Emerging(BaseModel):
    """One class that has grown thick enough to leave the pile.

    One name, deliberately. A reply that could carry several would be a partition, and a
    partition of a heterogeneous pile always needs somewhere to put the remainder.

    The verdict comes last. The model must first form the best concrete candidate, which
    avoids committing to a boolean before considering the evidence without paying for a
    free-form scratchpad.
    """

    axis: str = Field(
        default="",
        description=(
            "The short label for the documents' primary subject domain. It is not a "
            "comparison of candidate properties and not an explanation. Asked only the first time; "
            "after that the folder already has one and you are held to it."
        ),
    )
    axis_question: str = Field(
        default="",
        description=(
            "The question asking what primary subject domain the document is about. Every "
            "child folder name must be a direct answer to it. Asked only the first time."
        ),
    )
    name: str = Field(default="", description="Folder name for that class, one level. Not a path.")
    emerged: bool = Field(description="False when the concrete candidate is not worth a shelf.")

    @field_validator("axis")
    @classmethod
    def _axis_is_a_label(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("axis must be a single-line label")
        value = " ".join(value.split()).strip()
        if value and not is_axis_label(value):
            raise ValueError("axis must be a non-empty, single-line label")
        return value


class Members(BaseModel):
    """Which documents go under one named sign. The rest are not asked about."""

    document_ids: list[str] = Field(
        default_factory=list,
        description="Only the documents that belong under this sign. The rest stay where they are.",
    )


class NormalizedSign(BaseModel):
    """One umbrella subject sign replacing a joined root candidate, when one exists."""

    name: str = Field(
        default="",
        description="Shortest subject-domain noun phrase in the evidence language; no conjunction.",
    )
    valid: bool = Field(
        description="True only when one honest umbrella domain covers the joined candidate."
    )


class Group(BaseModel):
    """One sub-folder a division would create."""

    name: str = Field(description="Folder name, one level. Not a path.")
    note: str = Field(
        description=(
            "One short line positively describing only what belongs here. Do not discuss "
            "candidate axes, excluded documents, leftovers, or the classification process."
        )
    )
    document_ids: list[str] = Field(
        default_factory=list, description="The documents that go into this group."
    )


class Division(BaseModel):
    """The signs to put up, once it has been decided there should be some."""

    basis: str = Field(
        default="",
        description=(
            "The name of the one property the signs follow. Recorded on the folder and read "
            "back every time it is looked at again, so a sentence here is a sentence "
            "every later question is asked against."
        ),
    )
    basis_question: str = Field(default="")
    groups: list[Group] = Field(default_factory=list)
    replace_existing: bool = Field(default=False, exclude=True)
    reuse_existing: bool = Field(default=False, exclude=True)

    @field_validator("basis")
    @classmethod
    def _basis_is_a_label(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("basis must be a single-line label")
        value = " ".join(value.split()).strip()
        if value and not is_axis_label(value):
            raise ValueError("basis must be a non-empty, single-line label")
        return value


class Review(BaseModel):
    """Directly used checks of whether the current boundary still holds."""

    one_axis: bool = Field(
        description="All direct child signs still answer the one recorded axis question."
    )
    coherent_membership: bool = Field(
        description="Documents generally sit behind signs that accurately describe them."
    )
    useful_navigation: bool = Field(
        description="The current signs still help a reader rule alternatives out."
    )

    @property
    def holds(self) -> bool:
        return self.one_axis and self.coherent_membership and self.useful_navigation


class Replacement(BaseModel):
    """A complete new boundary, requested only after the current one fails."""

    basis: str = Field(
        default="",
        description=(
            "The name of the ONE property used by the complete replacement. Not a "
            "comparison between candidate properties and not an explanation."
        ),
    )
    basis_question: str = Field(
        default="",
        description="One question about one property that every replacement group answers.",
    )
    groups: list[Group] = Field(default_factory=list)

    @field_validator("basis")
    @classmethod
    def _basis_is_a_label(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("basis must be a single-line label")
        value = " ".join(value.split()).strip()
        if value and not is_axis_label(value):
            raise ValueError("basis must be a non-empty, single-line label")
        return value


class ReplacementSign(BaseModel):
    """One sign in a context-bounded replacement sketch; membership comes later."""

    name: str = Field(
        max_length=40,
        description=(
            "Short folder label, one level. Not a path, definition, explanation, or "
            "bilingual restatement."
        ),
    )

    @field_validator("name")
    @classmethod
    def _name_is_a_filesystem_sign(cls, value: str) -> str:
        value = " ".join(value.split()).strip()
        if re.search(r"\bD\d{4}\b", value, flags=re.IGNORECASE) or any(
            marker in value for marker in "[]/\\"
        ):
            raise ValueError("folder sign must not contain document handles or path syntax")
        return value


class ReplacementSketch(BaseModel):
    """A boundary design with no document IDs, safe to reduce across evidence packets."""

    basis: str = Field(
        max_length=120, description="The name of the one property used by every sign."
    )
    basis_question: str = Field(
        max_length=240, description="One question every sign name directly answers."
    )
    signs: list[ReplacementSign] = Field(min_length=2, max_length=12)

    @field_validator("basis")
    @classmethod
    def _basis_is_a_label(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("basis must be a single-line label")
        value = " ".join(value.split()).strip()
        if not is_axis_label(value):
            raise ValueError("basis must be a non-empty, single-line label")
        return value


class InitialBoundarySketch(ReplacementSketch):
    """Two or more sibling signs that establish a new folder's axis together."""


class ReplacementAssignment(BaseModel):
    """Membership for one displayed replacement-sign handle."""

    folder_id: str = Field(
        pattern=r"^G\d{3}$",
        description="One shown G### sign ID, copied exactly. No other handle is valid.",
    )
    document_ids: list[str] = Field(default_factory=list)

    @field_validator("folder_id", mode="before")
    @classmethod
    def _normalise_folder_id(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip().rstrip("/\\").strip().strip("[]").strip().upper()


class ReplacementAssignments(BaseModel):
    """Complete membership for one bounded document packet."""

    groups: list[ReplacementAssignment] = Field(default_factory=list)
    unassigned_document_ids: list[str] = Field(
        default_factory=list,
        description="Documents that genuinely fit no proposed sign; any make the plan abort.",
    )


class BoundaryAudit(BaseModel):
    """Independent semantic check before a proposed boundary reaches the filesystem."""

    one_property: bool = Field(
        description="The axis names one property, not a comparison or mixture of properties."
    )
    names_answer_question: bool = Field(
        description="Every proposed folder name is a direct answer to the axis question."
    )
    mutually_exclusive: bool = Field(
        description="A document does not naturally belong to several proposed sibling classes."
    )
    useful_for_navigation: bool = Field(
        description="The signs materially narrow the search instead of restating the documents."
    )
    members_match_signs: bool = Field(
        description="Every assigned document actually matches the sign it is assigned under."
    )
    no_remainder_sign: bool = Field(
        description="No sign is a miscellaneous, other, remainder, or everything-else bucket."
    )
    violations: list[
        Literal[
            "candidate_comparison",
            "mixed_axis",
            "name_not_answer",
            "overlap",
            "document_title_sign",
            "weak_navigation",
            "family_incompatible",
            "membership_mismatch",
            "remainder_sign",
        ]
    ] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Blocking violation codes only; never explain them in prose. Allowed codes: "
            "candidate_comparison, mixed_axis, name_not_answer, overlap, "
            "document_title_sign, weak_navigation, family_incompatible, "
            "membership_mismatch, remainder_sign."
        ),
    )

    @property
    def accepted(self) -> bool:
        return not self.violations and all(
            (
                self.one_property,
                self.names_answer_question,
                self.mutually_exclusive,
                self.useful_for_navigation,
                self.members_match_signs,
                self.no_remainder_sign,
            )
        )

    @model_validator(mode="after")
    def _violations_must_match_checks(self) -> BoundaryAudit:
        checks = {
            "candidate_comparison": self.one_property,
            "mixed_axis": self.one_property,
            "name_not_answer": self.names_answer_question,
            "overlap": self.mutually_exclusive,
            "family_incompatible": self.mutually_exclusive,
            "document_title_sign": self.useful_for_navigation,
            "weak_navigation": self.useful_for_navigation,
            "membership_mismatch": self.members_match_signs,
            "remainder_sign": self.no_remainder_sign,
        }
        contradictions = [code for code in self.violations if checks[code]]
        if contradictions:
            raise ValueError(
                "violation codes contradict true checks: " + ", ".join(contradictions)
            )
        return self


class ClassAudit(BaseModel):
    """Semantic check for one additive shelf, not for a complete boundary.

    A first shelf deliberately leaves unrelated documents loose.  Reusing
    ``BoundaryAudit`` here was contradictory because that schema judges two or more
    siblings and therefore treats a valid unary extraction as an incomplete boundary.
    """

    name_answers_question: bool = Field(
        description="The sign is a specific answer to the primary-subject question."
    )
    recurring_class: bool = Field(
        description="The sign names a reusable subject class, not one title or a vague umbrella."
    )
    useful_for_navigation: bool = Field(
        description="The sign and its valid members materially narrow the loose document list."
    )
    distinct_from_contrast: bool = Field(
        description=(
            "The sign does not also naturally describe the supplied unclaimed contrast "
            "documents or collapse existing sibling distinctions."
        )
    )
    invalid_member_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Only shown D#### handles assigned to the sign that do not actually have that "
            "primary subject. An empty list means every shown member fits."
        ),
    )


class ReplacementAudit(BaseModel):
    """Whether a replacement fixes the failed boundary and improves navigation."""

    fixes_observed_failure: bool = Field(
        description="The proposal directly fixes the failure found in the current boundary."
    )
    better_navigation: bool = Field(
        description="The proposed signs narrow search materially better than the current signs."
    )

    @property
    def accepted(self) -> bool:
        return self.fixes_observed_failure and self.better_navigation


class ExistingAssignment(BaseModel):
    """Documents routed through one opaque existing-folder handle."""

    folder_id: str = Field(description="One shown F### direct-child ID, copied exactly.")
    document_ids: list[str] = Field(default_factory=list)

    @field_validator("folder_id")
    @classmethod
    def _normalise_folder_id(cls, value: str) -> str:
        return value.strip().rstrip("/\\").strip().strip("[]").strip().upper()


class ExistingAssignments(BaseModel):
    """Loose documents that clearly belong behind signs already present."""

    groups: list[ExistingAssignment] = Field(
        default_factory=list,
        description=(
            "Only confident assignments to shown folder IDs; unassigned documents remain "
            "where they are."
        ),
    )


class RoutingAudit(BaseModel):
    """Independent check that a partial refiling follows the existing boundary."""

    assignments_match_signs: bool = Field(
        description="Each document clearly belongs under the existing sign named for it."
    )
    no_forced_fit: bool = Field(
        description="No document is assigned merely because one existing sign is closest."
    )

    @property
    def accepted(self) -> bool:
        return self.assignments_match_signs and self.no_forced_fit


def build_emerging(
    *,
    path: str,
    purpose: str,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
    axis: str = "",
    axis_question: str = "",
    spent: list[str] | None = None,
    recently_rejected: list[str] | None = None,
) -> Prompt:
    """Step one: has any one class grown thick enough to come out?

    With an ``axis``, the folder has been divided before and the question narrows to
    "another answer to the same question?". Without one, the axis is chosen here and
    every sub-folder this folder ever gets is held to it.
    """
    user = _listing(path, purpose, documents, children)
    if recently_rejected:
        user += (
            "\n\nRECENTLY TESTED CANDIDATES THAT DID NOT FORM A VALID RECURRING CLASS "
            "AT COMPARABLE EVIDENCE: "
            + ", ".join(recently_rejected)
            + "\nDo not repeat or paraphrase these candidates in this pass. Look for a "
            "different evidence-backed recurring class; otherwise return emerged=false."
        )
    hangul_documents = sum(bool(re.search(r"[가-힣]", description)) for _, description in documents)
    if documents and hangul_documents * 2 >= len(documents):
        user += (
            "\n\nOUTPUT LANGUAGE CONTRACT: The evidence is Korean. Write axis, axis_question, "
            "and name in Korean Hangul. The name must be a short subject noun phrase, not an "
            "English translation and not a phrase meaning law, regulation, framework, or documents."
        )
    if not path:
        user += (
            "\nROOT SIGN CONTRACT: Name exactly one broad subject domain with the shortest "
            "established noun phrase supported by the evidence. Do not join domains with 및, "
            "and, &, a slash, or a comparison. Omit generic legal-action words meaning law, "
            "regulation, framework, system, policy, documents, or protection when the subject "
            "domain itself remains clear."
        )
    if not axis:
        if spent:
            user += (
                "\n\nPROPERTIES ALREADY USED ABOVE THIS FOLDER (do not reuse them here):\n  "
                + "\n  ".join(spent)
            )
        return Prompt(system=_EMERGING_SYSTEM, user=user)
    return Prompt(
        system=_EMERGING_ALONG_SYSTEM,
        user=(
            f"{user}\n\nTHE RECORDED AXIS (copy, do not reinterpret): {axis}"
            f"\nTHE RECORDED QUESTION (answer exactly this question): {axis_question}"
        ),
    )


def build_initial_boundary_sketch(
    *,
    path: str,
    purpose: str,
    documents: list[tuple[str, str]],
    spent: list[str] | None = None,
) -> Prompt:
    """Design a first boundary from multiple sibling classes at once."""
    spent_text = (
        "\n\nPROPERTIES ALREADY USED ABOVE (do not reuse):\n  " + "\n  ".join(spent)
        if spent
        else ""
    )
    return Prompt(
        system=(
            "This folder has no child folders. A boundary cannot be established by one "
            "class. Return TWO OR MORE sibling signs that are direct, mutually exclusive "
            "answers to one stable semantic subject, purpose, or responsible-domain question. "
            "Document type, legal/formal hierarchy, file format, language, and date are metadata "
            "facets, not folder axes; NEVER use them as the basis or signs. Leave unrelated "
            "documents at the "
            "parent; never invent a remainder sign. The axis names one property, not a "
            "comparison of documents or candidate classes, and its question must remain "
            "meaningful for future documents. Each sign describes a recurring class rather "
            "than echoing one document. Documents sharing one ATOMIC_MEMBERS list are indivisible: "
            "choose an axis under which every such unit can stay in one sign or remain together "
            "at the parent. Sign names are short labels in the "
            "documents' own language: never include D#### handles, examples, translations, "
            "parentheses explaining the label, or prose definitions. Return no explanation."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nPURPOSE: {purpose or '(none)'}\n"
            f"DOCUMENTS:\n{_render_documents(documents)}{spent_text}"
        ),
    )


def build_members(
    *,
    path: str,
    purpose: str,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
    name: str,
    note: str,
) -> Prompt:
    """Step two: who goes on that shelf. Only reached when step one named one."""
    user = _listing(path, purpose, documents, children)
    return Prompt(system=_MEMBERS_SYSTEM, user=f"{user}\n\nTHE SHELF: {name} — {note}")


def build_normalized_root_sign(
    *, candidate: str, documents: list[tuple[str, str]] | None = None
) -> Prompt:
    """Reduce every root candidate to one honest top-level subject domain."""
    return Prompt(
        system=(
            "루트 폴더 후보 표지 하나를 검토한다. 루트 형제는 서로 겹치지 않는 최상위 "
            "주제 분야여야 한다. 후보를 문서 언어의 가장 짧고 확립된 주제 명사구로 "
            "정규화한다. 아래 증거에서 후보를 실제로 지지하는 반복 문서들을 찾고, 무관한 "
            "문서는 대비 자료로만 쓴다. 세부 대상·행위·정책을 붙인 하위 분야라면 그 "
            "반복 문서들의 의미를 정직하게 "
            "포괄하는 상위 주제 분야로 올린다. 법, 규제, 제도, 정책, 문서, 지원, 보호처럼 "
            "자료 전반에 흔한 행위·형식 단어는 분야를 구별하지 못하면 뺀다. 이미 하나의 "
            "짧은 통용 분야명이면 그 표준 어순을 그대로 둔다. 행위의 상태나 정책 목표를 "
            "설명하는 말은 분야명이 아니다. 문서 여러 개에 같은 소관·발행·감독 기관이 "
            "반복된다는 이유로 그 기관이나 '그 기관 소관 문서/법령'을 주제 분야로 삼지 "
            "않는다. 문서들이 실제로 규율하는 대상과 활동에 하나의 공통 분야가 없고 공통점이 "
            "책임 기관이나 법적 형식뿐이면 valid=false로 판단한다. name은 후보를 새 말로 "
            "재서술한 표현이 아니라 "
            "최소 두 개의 서로 다른 증거 행에 주제어로 실제 반복 등장하는 명사구에 "
            "근거해야 한다. 단어를 재배열하거나 유사어를 만들어내지 않는다. 두 분야 중 "
            "하나를 임의로 선택하거나 새 분류 "
            "체계를 만들지 않는다. 구체적인 탐색 범위를 주지 못하는 일반어이거나 하나의 "
            "포괄 분야가 없으면 valid=false와 빈 name을 반환한다. name에는 및, and, &, "
            "슬래시, 비교 표현을 쓰지 않는다. 입력 후보가 한글이면 name도 반드시 한글로 "
            "쓴다. 번역하지 말고 설명하지 않는다."
        ),
        user=(
            f"현재 후보 표지: {candidate}\n\n"
            "후보를 만들 때 사용한 문서 주제 증거:\n"
            f"{_render_documents(documents or []) or '  (없음)'}"
        ),
    )


def build_emerging_reduce(
    *,
    path: str,
    purpose: str,
    axis: str,
    axis_question: str = "",
    children: list[tuple[str, str]],
    candidates: list[Emerging],
) -> Prompt:
    """Choose one class from candidates discovered in isolated document packets."""
    rendered = "\n".join(
        f"  - axis={candidate.axis or axis} | question={candidate.axis_question} | "
        f"name={candidate.name}"
        for candidate in candidates
        if candidate.emerged
    )
    existing = _render_children(children) or "  (none)"
    axis_rule = (
        f"The recorded axis is {axis!r} and its immutable question is "
        f"{axis_question!r}; the selected candidate must answer that exact question."
        if axis
        else "Select one property shared by every future sibling."
    )
    return Prompt(
        system=(
            "Document packets independently proposed classes that may have emerged in one "
            "library folder. Select the strongest reusable class, resolving synonyms and "
            "discarding packet-local or document-title shelves. Return one candidate only, "
            "with no document IDs or explanation. " + axis_rule
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nPURPOSE: {purpose or '(none)'}\n"
            f"EXISTING SIGNS:\n{existing}\n\nPACKET CANDIDATES:\n{rendered}"
        ),
    )


def build_review(
    *,
    path: str,
    purpose: str,
    basis: str,
    basis_question: str,
    before: int,
    count: int,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
) -> Prompt:
    """Ask whether an existing division still holds."""
    return Prompt(
        system=_REVIEW_SYSTEM,
        user=_review_listing(
            path=path,
            purpose=purpose,
            basis=basis,
            basis_question=basis_question,
            before=before,
            count=count,
            documents=documents,
            children=children,
        ),
    )


def build_replacement_sketch(
    *,
    path: str,
    purpose: str,
    current_axis: str,
    current_question: str,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
) -> Prompt:
    """Propose signs from one bounded packet; document membership is assigned later."""
    return Prompt(
        system=(
            "The current library boundary failed review. Design a replacement boundary from "
            "this bounded evidence packet. This is one packet from a larger subtree, so do not "
            "return document IDs. Name one corpus-evidenced property and two or more reusable "
            "class signs on that property. Signs must be mutually exclusive, useful for ruling "
            "alternatives out, and written in the documents' own language. Notes are short "
            "positive routing rules, not inventories, counts, exclusions, or process narration. "
            "Do not import a domain taxonomy that is absent from the evidence."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nPURPOSE: {purpose or '(none)'}\n"
            f"FAILED AXIS: {current_axis}\nFAILED QUESTION: {current_question}\n"
            f"CURRENT DIRECT SIGNS:\n{_render_children(children) or '  (none)'}\n\n"
            f"EVIDENCE PACKET ({len(documents)} documents):\n{_render_documents(documents)}"
        ),
    )


def build_replacement_reduce(
    *, path: str, sketches: list[ReplacementSketch], initial: bool = False
) -> Prompt:
    """Reduce several isolated packet sketches into one coherent boundary design."""
    rendered = "\n\n".join(
        f"CANDIDATE {index}:\n  AXIS: {sketch.basis}\n  QUESTION: {sketch.basis_question}\n"
        + "\n".join(f"  - {sign.name}/" for sign in sketch.signs)
        for index, sketch in enumerate(sketches, start=1)
    )
    initial_rule = (
        "This establishes an initial folder axis: keep only semantic subject, purpose, or "
        "responsible-domain axes. Never select or reconstruct a document-type, legal/formal "
        "hierarchy, file-format, language, or date axis. "
        if initial
        else ""
    )
    return Prompt(
        system=(
            "Consolidate candidate library boundaries produced from separate evidence packets. "
            + initial_rule
            +
            "Return one boundary on one property whose signs can cover the classes evidenced "
            "across all candidates. Resolve synonyms and competing axes; do not mix axes or add "
            "a remainder class. Use the archive's own language. Return no document IDs and "
            "no explanation."
        ),
        user=f"FOLDER: {path or '(root)'}\n\nPACKET CANDIDATES:\n{rendered}",
    )


def build_replacement_assignments(
    *,
    path: str,
    documents: list[tuple[str, str]],
    sketch: ReplacementSketch,
) -> Prompt:
    """Assign one bounded packet completely after the global signs are fixed."""
    signs = "\n".join(
        f"  [G{index:03d}] {sign.name}/" for index, sign in enumerate(sketch.signs, start=1)
    )
    return Prompt(
        system=(
            "Assign every shown document to exactly one proposed library sign. Copy only G### "
            "handles and D#### document handles exactly. Do not rename or create signs. If a "
            "document genuinely fits no sign, put its ID in unassigned_document_ids; never force "
            "the nearest fit. ATOMIC_MEMBERS is a constraint, not a destination: all listed "
            "documents must choose the same G### sign or all remain unassigned. Never return a "
            "path, dot, family/unit label, or anything except a shown G### as folder_id. Return "
            "no explanation."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {sketch.basis}\n"
            f"QUESTION: {sketch.basis_question}\nSIGNS:\n{signs}\n\n"
            f"DOCUMENT PACKET ({len(documents)}):\n{_render_documents(documents)}"
        ),
    )


def build_existing_choice(
    *,
    path: str,
    document: tuple[str, str],
    axis: str,
    axis_question: str,
    children: list[tuple[str, str]],
) -> Prompt:
    """Route one loose document with a closed, non-JSON decision."""
    signs = "\n".join(
        f"  [F{index:03d}] {name}/" for index, (name, _) in enumerate(children, start=1)
    )
    return Prompt(
        system=(
            "Route this one loose document only when an existing sign positively describes it. "
            "Reply with exactly one shown F### handle or NEW_SIBLING. NEW_SIBLING is normal when "
            "a more accurate natural top-level subject is not shown; never select the closest "
            "merely related sign. An organization's own structure, staffing, offices, jurisdiction, "
            "establishment, or supervision is institutional administration, not the policy or "
            "industry subject embedded in the organization's name. Do not explain, rename, or "
            "create a sign."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"SIGNS:\n{signs}\n\nDOCUMENT:\n  [{document[0]}] {document[1]}"
        ),
    )


def build_member_choice(*, path: str, purpose: str, document: tuple[str, str], name: str) -> Prompt:
    """Decide membership in one new class without echoing a document ID."""
    return Prompt(
        system=(
            "A new library class has been named. Decide whether this one document genuinely "
            "belongs behind that sign. Reply with exactly SHELF or STAY. Do not explain. STAY "
            "means the document remains safely in its current folder."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nPURPOSE: {purpose or '(none)'}\n"
            f"NEW SIGN: {name}/\n\nDOCUMENT:\n  [{document[0]}] {document[1]}"
        ),
    )


def build_replacement_choice(
    *, path: str, document: tuple[str, str], sketch: ReplacementSketch
) -> Prompt:
    """Assign one document to one fixed replacement sign."""
    signs = "\n".join(
        f"  [G{index:03d}] {sign.name}/" for index, sign in enumerate(sketch.signs, start=1)
    )
    return Prompt(
        system=(
            "A complete replacement boundary has already been fixed. Assign this one document "
            "to exactly one shown sign. Reply with exactly one G### handle. Do not explain, "
            "rename, or create a sign."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {sketch.basis}\n"
            f"QUESTION: {sketch.basis_question}\nSIGNS:\n{signs}\n\n"
            f"DOCUMENT:\n  [{document[0]}] {document[1]}"
        ),
    )


def build_boundary_audit(
    *,
    path: str,
    documents: list[tuple[str, str]],
    axis: str,
    axis_question: str,
    groups: list[Group],
    complete: bool,
) -> Prompt:
    """Verify semantics without supplying a domain taxonomy or preferred axis."""
    rendered_groups = "\n".join(
        f"  {group.name}/ — {group.note} — ids: {', '.join(group.document_ids)}" for group in groups
    )


    mode = (
        "This replaces the whole boundary, so every document must be represented."
        if complete
        else "This draws out one class; unclaimed documents intentionally remain loose."
    )
    return Prompt(
        system=(
            "You are the adversarial verifier for a proposed library boundary. First try to "
            "falsify it and record every concrete blocking issue in `violations`. Judge only "
            "from the supplied documents and proposal. Do not introduce a preferred domain "
            "taxonomy. The axis must name one property, its question must ask only that "
            "property, and every sibling name must be an answer to that question. Reject "
            "candidate comparisons, mixed axes, overlapping siblings, document-title shelves, "
            "miscellaneous/remainder signs, assigned documents that do not actually match their "
            "sign, and distinctions that do not help a reader rule alternatives out. Do not mark "
            "membership valid merely because related documents were kept together: each member "
            "must still truthfully satisfy the chosen sign. A new boundary "
            "with fewer than two sibling signs cannot establish an axis. Folder notes "
            "are derived by the application and are not part of this judgement."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nMODE: {mode}\nAXIS: {axis}\n"
            f"QUESTION: {axis_question}\nGROUPS:\n{rendered_groups}\n\n"
            f"DOCUMENTS:\n{_render_documents(documents)}"
        ),
    )


def build_rebalance_choice(
    *,
    path: str,
    current: str,
    document: tuple[str, str],
    axis: str,
    axis_question: str,
    children: list[tuple[str, str]],
) -> Prompt:
    """Review one existing filing against the now-complete sibling contrast."""
    signs = "\n".join(
        f"  [F{index:03d}] {name}/" for index, (name, _) in enumerate(children, start=1)
    )
    return Prompt(
        system=(
            "Review one existing top-level filing after more sibling signs became available. "
            "Reply with exactly one shown F### handle or KEEP. KEEP is the conservative default. "
            "Choose another handle only when it is a clearly more accurate natural parent for the "
            "document's primary subject, not merely another related subject, regulator, beneficiary, "
            "technology, payment mechanism, or secondary effect. If the best natural subject is not "
            "shown, reply KEEP. Do not explain, rename, or create a sign."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"CURRENT SIGN: {current}/\nSIGNS:\n{signs}\n\n"
            f"DOCUMENT:\n  [{document[0]}] {document[1]}"
        ),
    )


def build_rebalance_comparison(
    *, current: str, proposed: str, document: tuple[str, str]
) -> Prompt:
    """Require an explicit departure judgement before moving an existing filing."""
    return Prompt(
        system=(
            "Compare the current and proposed library signs for one document's PRIMARY SUBJECT. "
            "Reply MOVE only when the proposed sign is clearly more accurate and the current sign "
            "is materially misleading. Relatedness or a small improvement is not enough. Reply KEEP "
            "when either sign is defensible, when the distinction is ambiguous, or when an unshown "
            "third sign would be better. Reply exactly MOVE or KEEP with no explanation."
        ),
        user=(
            f"CURRENT SIGN: {current}/\nPROPOSED SIGN: {proposed}/\n\n"
            f"DOCUMENT:\n  [{document[0]}] {document[1]}"
        ),
    )


def build_class_audit(
    *,
    path: str,
    axis: str,
    axis_question: str,
    name: str,
    total_loose_documents: int,
    total_claimed_members: int,
    members: list[tuple[str, str]],
    contrast: list[tuple[str, str]],
    siblings: list[tuple[str, str]],
) -> Prompt:
    """Audit one additive class and identify only its false-positive members."""
    return Prompt(
        system=(
            "Audit ONE additive library shelf. This is intentionally not a complete boundary: "
            "unclaimed documents remain loose, and no second sibling is required. Judge the sign "
            "as an answer to the stated primary-subject question. It must name a recurring, "
            "specific subject class and materially narrow navigation. For membership, return only "
            "shown D#### handles whose PRIMARY SUBJECT does not match the sign. Relatedness, a "
            "mentioned authority, or a secondary consumer/finance aspect is not enough. When the "
            "question asks for a subject or domain, a responsible, issuing, supervising, or owning "
            "organization is not itself the subject class, and a collection such as 'documents or "
            "laws under that organization' is invalid unless the documents independently share the "
            "named regulated subject. Mark as invalid any member whose primary subject is the "
            "organization's own structure, staffing, offices, jurisdiction, establishment, or "
            "supervision merely because the organization's name contains the proposed sign. Do not "
            "reject the whole class merely because some members are invalid; identify those IDs. "
            "Separately reject broad umbrellas that also naturally describe the unclaimed contrast "
            "documents or erase the distinction made by an existing sibling sign. Do not audit the "
            "old siblings' document membership; their names are context only."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"ONE PROPOSED SIGN: {name}/\nTOTAL LOOSE DOCUMENTS: {total_loose_documents}\n"
            f"TOTAL CLAIMED MEMBERS: {total_claimed_members}\n\n"
            f"EXISTING SIBLING SIGNS:\n{_render_children(siblings) or '  (none)'}\n\n"
            f"CLAIMED MEMBERS IN THIS PACKET:\n{_render_documents(members)}\n\n"
            f"UNCLAIMED CONTRAST DOCUMENTS (must stay outside this sign):\n"
            f"{_render_documents(contrast) or '  (none)'}"
        ),
    )


def build_member_fit_audit(
    *,
    path: str,
    axis: str,
    axis_question: str,
    name: str,
    document: tuple[str, str],
) -> Prompt:
    """Verify one claimed member in isolation before the harness moves it."""
    return Prompt(
        system=(
            "Verify one proposed library-shelf membership in isolation. Reply BELONG only "
            "when the sign is an accurate natural parent category for the document's PRIMARY "
            "SUBJECT; a more specific subtype does not need to repeat the sign's wording. "
            "Interpret the entire sign, including every meaningful modifier. Shared words, a "
            "regulator, consumer implications, financing, or another secondary aspect are not "
            "sufficient. When the axis asks for subject or domain, being issued, owned, or supervised "
            "by the organization named in the sign is not membership. A document about that "
            "organization's own structure, staffing, offices, jurisdiction, establishment, or "
            "supervision is institutional administration, not the policy/industry domain embedded "
            "in its name. For statutes and regulations, "
            "the subject named in the title and the "
            "explicit purpose are the strongest evidence. Do not reclassify a law by an affected "
            "or beneficiary population when its named regulated activity is different. Reply "
            "STAY when the document's primary subject belongs to a neighboring future class. "
            "Reply exactly BELONG or STAY with no explanation."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"PROPOSED SIGN: {name}/\n\nONE CLAIMED DOCUMENT:\n"
            f"  [{document[0]}] {document[1]}"
        ),
    )


def build_member_dispute_audit(
    *,
    path: str,
    axis: str,
    axis_question: str,
    name: str,
    document: tuple[str, str],
) -> Prompt:
    """Resolve only a disagreement between the aggregate and isolated reviewers."""
    return Prompt(
        system=(
            "Resolve a disputed library-shelf membership. One reviewer accepted this "
            "document and another identified it as a false positive. Recheck conservatively. "
            "Reply BELONG only when the sign names the document's primary regulated subject "
            "or its natural parent domain. Similar words such as protection, support, fairness, "
            "finance, or administration do not make different protected populations, regulated "
            "activities, or industries the same class. For statutes, the title and explicit "
            "purpose outrank beneficiaries, agencies, sanctions, and incidental topics. Internal "
            "organization, staffing, offices, jurisdiction, establishment, or supervision is not "
            "the organization's named policy domain. If a "
            "more honest future sibling can be named, reply STAY. Reply exactly BELONG or STAY."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"DISPUTED SIGN: {name}/\n\nONE DISPUTED DOCUMENT:\n"
            f"  [{document[0]}] {document[1]}"
        ),
    )


def build_replacement_audit(
    *,
    path: str,
    documents: list[tuple[str, str]],
    current_axis: str,
    current_question: str,
    current_children: list[tuple[str, str]],
    observed_failures: list[str],
    proposed_axis: str,
    proposed_question: str,
    proposed_groups: list[Group],
) -> Prompt:
    """Compare a valid replacement with the boundary readers already learned."""
    old = _render_children(current_children) or "  (none)"
    new = "\n".join(
        f"  {group.name}/ — {group.note} — ids: {', '.join(group.document_ids)}"
        for group in proposed_groups
    )
    return Prompt(
        system=(
            "You are the final change-control reviewer for a library boundary. The proposed "
            "boundary is already structurally valid; decide whether replacing the current "
            "one is materially better. Existing locations have learned value and moving them "
            "has a cost. Another plausible taxonomy is not enough. Approve only when the new "
            "boundary directly fixes the observed failure and narrows search substantially "
            "better. Those two checks already express whether disruption is justified; do not "
            "add a separate preference for preserving a boundary that has failed. Do not prefer "
            "any domain taxonomy."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nCURRENT AXIS: {current_axis}\n"
            f"CURRENT QUESTION: {current_question}\n"
            f"OBSERVED FAILURES: {', '.join(observed_failures) or '(none)'}\n"
            f"CURRENT SIGNS:\n{old}\n\n"
            f"PROPOSED AXIS: {proposed_axis}\nPROPOSED QUESTION: {proposed_question}\n"
            f"PROPOSED SIGNS:\n{new}\n\nDOCUMENTS:\n{_render_documents(documents)}"
        ),
    )


def build_existing_assignments(
    *,
    path: str,
    documents: list[tuple[str, str]],
    axis: str,
    axis_question: str,
    children: list[tuple[str, str]],
) -> Prompt:
    """Ask whether loose documents already belong behind an existing direct sign."""
    handled_children = [
        (f"F{index:03d}", name, note) for index, (name, note) in enumerate(children, start=1)
    ]
    rendered_children = "\n".join(
        f"  [{folder_id}] {name}/" + (f" — {note}" if note else "")
        for folder_id, name, note in handled_children
    )
    return Prompt(
        system=(
            "This folder already has sub-folders along one recorded axis. Route only loose "
            "documents that clearly belong under an existing direct sub-folder. Return only "
            "the shown F### handle; never copy or compose a folder name. Do not create or "
            "rename a class, change the "
            "axis, or account for every document. An empty result is normal. A document "
            "that merely fits one sign better than the others stays loose."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"EXISTING DIRECT SUB-FOLDERS:\n{rendered_children or '  (none)'}\n\n"
            f"LOOSE DOCUMENTS:\n{_render_documents(documents)}"
        ),
    )


def build_routing_audit(
    *,
    path: str,
    documents: list[tuple[str, str]],
    axis: str,
    axis_question: str,
    groups: list[Group],
    children: list[tuple[str, str]],
) -> Prompt:
    """Verify a proposed partial refiling without inventing a taxonomy."""
    assignments = "\n".join(
        f"  {group.name}/ <- {', '.join(group.document_ids)}" for group in groups
    )
    return Prompt(
        system=(
            "Independently verify a partial refiling into existing library signs. Judge only "
            "from the supplied evidence and recorded axis. Reject forced closest-match filing, "
            "mixed-axis reasoning, and assignments not directly described by the target sign. "
            "Do not require every loose document to be assigned."
        ),
        user=(
            f"FOLDER: {path or '(root)'}\nAXIS: {axis}\nQUESTION: {axis_question}\n"
            f"EXISTING DIRECT SUB-FOLDERS:\n{_render_children(children) or '  (none)'}\n"
            f"PROPOSED ASSIGNMENTS:\n{assignments}\n\n"
            f"LOOSE DOCUMENTS:\n{_render_documents(documents)}"
        ),
    )


def _listing(
    path: str, purpose: str, documents: list[tuple[str, str]], children: list[tuple[str, str]]
) -> str:
    return _LISTING.format(
        path=path or "(루트)",
        purpose=f"NOTE: {purpose}\n" if purpose else "",
        count=len(documents),
        documents=_render_documents(documents),
        children=_render_children(children),
    )


def _review_listing(
    *,
    path: str,
    purpose: str,
    basis: str,
    basis_question: str,
    before: int,
    count: int,
    documents: list[tuple[str, str]],
    children: list[tuple[str, str]],
) -> str:
    return _REVIEW_USER.format(
        path=path or "(루트)",
        purpose=f"NOTE: {purpose}\n" if purpose else "",
        basis=basis,
        basis_question=basis_question or "(not recorded)",
        before=before,
        count=count,
        loose=len(documents),
        documents=_render_documents(documents),
        children=_render_children(children) or "  (none)",
    )


def _render_documents(documents: list[tuple[str, str]]) -> str:
    if not documents:
        return "  (none)"
    return "\n".join(f"  [{document_id}] {line}" for document_id, line in documents)


def _render_children(children: list[tuple[str, str]]) -> str:
    if not children:
        return ""
    rendered = "\n".join(
        f"  {name}/  — {note}" if note else f"  {name}/" for name, note in children
    )
    return f"EXISTING SUB-FOLDERS:\n{rendered}"
