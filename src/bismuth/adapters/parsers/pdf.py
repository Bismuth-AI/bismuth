"""PDF extraction via pypdf (BSD), not PyMuPDF (AGPL); reads text only, not layout, and scanned pages yield nothing."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from bismuth.adapters.parsers.registry import build_extraction
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

    def parse(self, path: Path, *, max_chars: int) -> Extraction:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - exercised by install shape, not tests
            raise ParserUnavailableError(
                "Reading PDFs needs pypdf: pip install 'bismuth-kb[parsers]'"
            ) from exc

        try:
            reader = PdfReader(str(path))
            page_count = len(reader.pages)
        except Exception as exc:
            raise ParserUnavailableError(f"{path.name} is not a readable PDF: {exc}") from exc

        extraction = build_extraction(
            _pages(reader), parser=self.name, max_chars=max_chars, page_count=page_count
        )

        if page_count and not extraction.text.strip():
            # Almost always a scan; better to say so than emit a confident card about it.
            raise ParserUnavailableError(
                f"{path.name}: {page_count} pages, no extractable text. This is most "
                f"likely a scanned PDF. Bismuth has no OCR yet; run one over it first."
            )
        return extraction


def _pages(reader: object) -> Iterator[Section]:
    for index, page in enumerate(reader.pages):  # type: ignore[attr-defined]
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            yield Section(heading=None, text=text.strip(), page=index + 1, order=index)
