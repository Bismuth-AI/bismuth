"""Document parser adapters."""

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
    """Build the default parser registry in precedence order."""
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
