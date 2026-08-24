"""Derived document-state boundary."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from bismuth.domain.document import DocumentCard, SourceRef


@runtime_checkable
class Catalog(Protocol):
    """Derived document metadata for a vault."""

    def load_card(self, document_id: str) -> DocumentCard | None: ...

    def save_card(self, document_id: str, card: DocumentCard, *, source: SourceRef) -> None: ...

    def forget(self, document_id: str) -> None:
        """Drop a card when its document is deleted."""
        ...

    def load_source(self, document_id: str) -> SourceRef | None:
        """Load the source metadata captured during ingestion."""
        ...

    def iter_cards(self) -> Iterator[tuple[str, DocumentCard]]: ...

    def card_count(self) -> int: ...

    def find_by_hash(self, sha256: str) -> str | None:
        """Document id for content already ingested (content-addressed), if any."""
        ...
