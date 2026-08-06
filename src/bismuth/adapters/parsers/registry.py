"""Parser dispatch, and the accounting that keeps truncation honest."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import ModuleType

from bismuth.domain.document import Extraction, Section
from bismuth.domain.errors import ParserUnavailableError
from bismuth.ports.parser import DocumentParser

logger = logging.getLogger(__name__)


def require(module: str, hint: str) -> ModuleType:
    """Import an optional parser dependency, or say how to install it."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ParserUnavailableError(hint) from exc


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

    def warm(self) -> dict[str, str]:
        """Import every parser's dependency now, so a missing extra is a boot-time line
        in the log instead of a surprise on somebody's first upload."""
        unavailable: dict[str, str] = {}
        for parser in dict.fromkeys(self._by_extension.values()):
            try:
                parser.warm()
            except ParserUnavailableError as exc:
                # Not fatal: a minimal install is a supported way to run this.
                unavailable[parser.name] = str(exc)
                logger.warning("parser %s is unavailable: %s", parser.name, exc)
        return unavailable


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
