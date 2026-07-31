"""Parser dispatch, and the accounting that keeps truncation honest."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from bismuth.domain.document import Extraction, Section
from bismuth.domain.errors import ParserUnavailableError
from bismuth.ports.parser import DocumentParser


class ExtensionRegistry:
    """Maps a file extension to the parser that reads it (extension-based, not content-sniffed)."""

    def __init__(self, parsers: Iterable[DocumentParser]) -> None:
        self._by_extension: dict[str, DocumentParser] = {}
        for parser in parsers:
            for extension in parser.extensions:
                # First registration wins: the container's ordering is the precedence rule.
                self._by_extension.setdefault(extension, parser)

    def for_path(self, path: Path) -> DocumentParser:
        parser = self._by_extension.get(path.suffix.lower())
        if parser is None:
            known = ", ".join(sorted(self._by_extension)) or "(none)"
            raise ParserUnavailableError(
                f"No parser for '{path.suffix or path.name}'. Supported: {known}. "
                f"Document parsers are an optional extra: pip install 'bismuth-kb[parsers]'"
            )
        return parser

    def supported_extensions(self) -> frozenset[str]:
        return frozenset(self._by_extension)


def build_extraction(
    sections: Iterator[Section] | Iterable[Section],
    *,
    parser: str,
    max_chars: int,
    page_count: int | None = None,
) -> Extraction:
    """Collect sections up to a character budget, recording whether extraction was truncated."""
    kept: list[Section] = []
    budget = max_chars
    truncated = False

    for section in sections:
        if budget <= 0:
            truncated = True
            break
        if len(section.text) > budget:
            kept.append(section.model_copy(update={"text": section.text[:budget]}))
            truncated = True
            break
        kept.append(section)
        budget -= len(section.text)

    return Extraction(
        sections=tuple(kept),
        parser=parser,
        page_count=page_count,
        truncated=truncated,
    )
