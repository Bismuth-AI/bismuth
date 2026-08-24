"""Document parsing boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from bismuth.domain.document import Extraction


@runtime_checkable
class DocumentParser(Protocol):
    """Reads one family of file formats into text."""

    @property
    def name(self) -> str:
        """Return the parser name recorded on each extraction."""
        ...

    @property
    def extensions(self) -> frozenset[str]:
        """Lowercase suffixes including the dot, e.g. ``{'.pdf'}``."""
        ...

    def parse(self, path: Path, *, max_chars: int) -> Extraction:
        """Extract text.

        ``max_chars`` guards against exhausting memory; truncation must be recorded
        on the extraction, never silent.

        Raises:
            ParserUnavailableError: if an optional dependency is missing.
        """
        ...

    def warm(self) -> None:
        """Verify that the parser is ready for use.

        Raises:
            ParserUnavailableError: if the parser cannot be loaded.
        """
        ...


@runtime_checkable
class ParserRegistry(Protocol):
    """Dispatches a file to the parser that can read it."""

    def for_path(self, path: Path) -> DocumentParser:
        """Raises ParserUnavailableError if nothing handles this extension."""
        ...

    def supported_extensions(self) -> frozenset[str]:
        """Return every file extension handled by the registry."""
        ...

    def warm(self) -> dict[str, str]:
        """Return unavailable parser names mapped to their load errors."""
        ...
