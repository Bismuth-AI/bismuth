"""The derived-state boundary -- everything in ``.bismuth/`` except the journal."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from bismuth.domain.document import DocumentCard, SourceRef
from bismuth.domain.placement import Placement


@runtime_checkable
class Catalog(Protocol):
    """Derived state about a vault: what each document is, and where it went."""

    def load_card(self, document_id: str) -> DocumentCard | None: ...

    def save_card(self, document_id: str, card: DocumentCard, *, source: SourceRef) -> None: ...

    def forget(self, document_id: str) -> None:
        """Drop a card and its placement. Called when the document is deleted."""
        ...

    def load_source(self, document_id: str) -> SourceRef | None:
        """What the document was when we first read it. Used to re-file without re-reading."""
        ...

    def iter_cards(self) -> Iterator[tuple[str, DocumentCard]]: ...

    def card_count(self) -> int: ...

    def find_by_hash(self, sha256: str) -> str | None:
        """Document id for content already ingested (content-addressed), if any."""
        ...

    def save_placement(self, placement: Placement) -> None: ...

    def load_placement(self, document_id: str) -> Placement | None: ...
