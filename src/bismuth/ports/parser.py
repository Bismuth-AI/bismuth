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

    def warm(self) -> None:
        """Import this parser's third-party dependency now.

        Parsers import lazily because they are an optional extra, but a server that
        finds out on the first upload is a server that said it was ready too early.
        Called once at startup so a missing extra is a boot-time line in the log.

        Raises:
            ParserUnavailableError: if the optional dependency is missing.
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

    def warm(self) -> dict[str, str]:
        """Import every registered parser's dependency. Returns parser name -> why it is
        unavailable, for the ones that are; an empty mapping means everything loaded."""
        ...
