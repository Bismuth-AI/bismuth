"""Page-oriented PDF text extraction."""

from __future__ import annotations

from pathlib import Path

from bismuth.adapters.parsers.registry import build_extraction, require
from bismuth.domain.document import Extraction, Section
from bismuth.domain.errors import ParserUnavailableError


class PdfParser:
    """Page-by-page text extraction. One section per page, so citations have a page number."""

    @property
    def name(self) -> str:
        return "pypdf"

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".pdf"})

    def warm(self) -> None:
        require("pypdf", "Reading PDFs needs pypdf: pip install 'bismuth-kb[parsers]'")

    def parse(self, path: Path, *, max_chars: int) -> Extraction:
        self.warm()
        from pypdf import PdfReader

        try:
            reader = PdfReader(str(path))
            page_count = len(reader.pages)
        except Exception as exc:
            raise ParserUnavailableError(f"{path.name} is not a readable PDF: {exc}") from exc

        pages, incomplete = _pages(reader)
        extraction = build_extraction(
            pages, parser=self.name, max_chars=max_chars, page_count=page_count
        )
        if incomplete:
            extraction = extraction.model_copy(update={"truncated": True})

        if page_count and not extraction.text.strip():
            raise ParserUnavailableError(
                f"{path.name}: {page_count} pages, no extractable text. This is most "
                f"likely a scanned PDF. Bismuth has no OCR yet; run one over it first."
            )
        return extraction


def _pages(reader: object) -> tuple[list[Section], bool]:
    sections: list[Section] = []
    incomplete = False
    for index, page in enumerate(reader.pages):  # type: ignore[attr-defined]
        try:
            text = page.extract_text() or ""
        except Exception:
            incomplete = True
            text = ""
        if text.strip():
            sections.append(Section(heading=None, text=text.strip(), page=index + 1, order=index))
    return sections, incomplete
