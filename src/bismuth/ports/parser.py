"""The parsing boundary. Synchronous and CPU-bound; services run it off the event loop."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from bismuth.domain.document import Extraction


@runtime_checkable
class DocumentParser(Protocol):
    """Reads one family of file formats into text."""

    @property
    def name(self) -> str:
        """Recorded on every :class:`~bismuth.domain.document.Extraction`."""
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


@runtime_checkable
class ParserRegistry(Protocol):
    """Dispatches a file to the parser that can read it."""

    def for_path(self, path: Path) -> DocumentParser:
        """Raises ParserUnavailableError if nothing handles this extension."""
        ...

    def supported_extensions(self) -> frozenset[str]:
        """Used by the CLI and the upload form, so the two cannot disagree."""
        ...
