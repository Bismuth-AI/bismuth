"""Document parsers; all under permissive licences (PyMuPDF/AGPL is deliberately excluded)."""

from bismuth.adapters.parsers.hwpx import HwpxParser
from bismuth.adapters.parsers.office import DocxParser, PptxParser, XlsxParser
from bismuth.adapters.parsers.pdf import PdfParser
from bismuth.adapters.parsers.plain import CsvParser, PlainTextParser
from bismuth.adapters.parsers.registry import ExtensionRegistry, build_extraction

__all__ = [
    "CsvParser",
    "DocxParser",
    "ExtensionRegistry",
    "HwpxParser",
    "PdfParser",
    "PlainTextParser",
    "PptxParser",
    "XlsxParser",
    "build_extraction",
    "build_registry",
]


def build_registry() -> ExtensionRegistry:
    """The default parser set; order is precedence, first parser claiming an extension keeps it."""
    return ExtensionRegistry(
        [
            PlainTextParser(),
            CsvParser(),
            PdfParser(),
            DocxParser(),
            PptxParser(),
            XlsxParser(),
            HwpxParser(),
        ]
    )
