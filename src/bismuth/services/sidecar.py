"""Renders a document's sidecar: the Markdown file that makes it greppable."""

from __future__ import annotations

from datetime import UTC, datetime

import yaml

from bismuth.domain.document import Coverage, DocumentCard, Extraction, SourceRef

SIDECAR_SCHEMA_VERSION = 1

_NOTICE = (
    "<!-- 옆에 있는 원본 파일에서 Bismuth 가 생성했습니다. 지워도 됩니다. "
    "원본은 절대 수정되지 않습니다. -->"
)


def read_sidecar_meta(text: str) -> dict[str, object] | None:
    """Recover a sidecar's frontmatter, or ``None`` if this is not one."""
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].rstrip() in ("---", "..."))
    except StopIteration:
        return None
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict) or "bismuth_sidecar" not in loaded:
        return None
    return loaded


def render_sidecar(
    *,
    source: SourceRef,
    card: DocumentCard,
    extraction: Extraction,
    document_id: str,
) -> str:
    """Render the full sidecar for one document."""
    return "\n".join(
        [
            _frontmatter(source=source, card=card, extraction=extraction, document_id=document_id),
            "",
            _NOTICE,
            "",
            _header(source=source, card=card, extraction=extraction),
            "",
            _body(card=card, extraction=extraction),
        ]
    )


def _frontmatter(
    *, source: SourceRef, card: DocumentCard, extraction: Extraction, document_id: str
) -> str:
    """Machine-readable metadata, duplicated from the catalog so it survives the catalog being deleted."""
    meta = {
        "bismuth_sidecar": SIDECAR_SCHEMA_VERSION,
        "document_id": document_id,
        "source": source.filename,
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "modified_at": source.modified_at.isoformat(),
        "parser": extraction.parser,
        "truncated": extraction.truncated,
        "title": card.title,
        "doc_type": card.doc_type,
        "language": card.language,
        "topics": list(card.topics),
        "entities": [{"name": e.name, "kind": e.kind.value} for e in card.entities],
        "keywords": list(card.keywords),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    if card.coverage is not None:
        meta["coverage"] = card.coverage.model_dump()
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{dumped}---"


def _header(*, source: SourceRef, card: DocumentCard, extraction: Extraction) -> str:
    """The block that stops a grep hit from being an orphan."""
    lines = [f"# {card.title}", ""]

    facts: list[str] = [f"**{card.doc_type}**"]
    if card.topics:
        facts += list(card.topics)
    if names := [e.name for e in card.entities][:5]:
        facts.append(", ".join(names))
    lines += ["> " + " · ".join(facts)]

    origin = f"> 원본: [{source.filename}]({source.filename})"
    if extraction.page_count:
        origin += f" — {extraction.page_count}쪽"
    lines += [origin]

    lines += _coverage_lines(card=card, extraction=extraction)

    lines += ["", "## 요약", "", card.summary]

    if card.answers_questions:
        lines += ["", "## 이 문서로 답할 수 있는 질문", ""]
        lines += [f"- {question}" for question in card.answers_questions]

    return "\n".join(lines)


def _coverage_lines(*, card: DocumentCard, extraction: Extraction) -> list[str]:
    """How much of the document the summary above is based on. Silence would read as 'all of it'."""
    coverage = card.coverage
    if coverage is None:
        # A card from before coverage existed; the parser flag is all we know.
        if not extraction.truncated:
            return []
        return [
            ">",
            "> ⚠️ **일부만 읽었습니다.** 이 문서가 추출 한도보다 길어서 아래는 앞부분뿐입니다.",
        ]

    if coverage.whole_document:
        return [f"> 📖 {_coverage_summary(coverage)}"]

    lines = [">", f"> ⚠️ **{_coverage_summary(coverage)}**"]
    if coverage.extraction_truncated:
        lines.append("> 추출 한도에 걸려 파일 뒷부분은 아예 읽지 못했습니다.")
    if coverage.windows_read < coverage.windows_total:
        lines.append("> 읽은 조각은 문서 전체에 고르게 흩어져 있지만, 사이사이는 건너뛰었습니다.")
    if coverage.windows_failed:
        lines.append(f"> 조각 {coverage.windows_failed}개는 모델이 읽지 못했습니다.")
    return lines


def _coverage_summary(coverage: Coverage) -> str:
    """Render document coverage for a sidecar."""
    if coverage.whole_document:
        return f"전체를 읽었습니다 ({coverage.chars_total:,}자, {coverage.windows_total}조각)"
    return (
        f"{coverage.windows_total}조각 중 {coverage.windows_read}조각을 읽었습니다 "
        f"({coverage.chars_read:,}/{coverage.chars_total:,}자)"
    )


def _body(*, card: DocumentCard, extraction: Extraction) -> str:
    """The extracted text: the thing grep actually matches."""
    lines = ["---", "", "## 본문", ""]

    for section in extraction.sections:
        if section.heading:
            lines += [f"### {section.heading}", ""]
        elif section.page is not None:
            lines += [f"### {section.page}쪽", ""]
        lines += [section.text, ""]

    if not extraction.sections:
        lines += ["_이 문서에서 텍스트를 추출하지 못했습니다._", ""]

    if card.keywords:
        lines += ["---", "", f"_키워드: {', '.join(card.keywords)}_"]

    return "\n".join(lines)
