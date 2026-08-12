"""Incremental maintenance is bounded by execution context, not corpus semantics."""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock

from bismuth.domain.document import DocumentCard
from bismuth.ports.catalog import Catalog
from bismuth.services.maintenance_windows import next_window, window_ready


def _card(summary: str = "compact") -> DocumentCard:
    return DocumentCard(
        title="document",
        summary=summary,
        doc_type="note",
        topics=("topic",),
    )


def _catalog(card: DocumentCard) -> Catalog:
    catalog = Mock()
    catalog.load_card.return_value = card
    return cast(Catalog, catalog)


def test_one_window_never_contains_more_than_fifty_arrivals() -> None:
    catalog = _catalog(_card())
    document_ids = [f"doc-{index}" for index in range(80)]

    window = next_window(catalog, document_ids)

    assert len(window) == 50
    assert window_ready(catalog, document_ids)


def test_verbose_cards_close_a_window_before_the_count_limit() -> None:
    catalog = _catalog(_card("x" * 7_000))
    document_ids = ["one", "two", "three", "four"]

    window = next_window(catalog, document_ids, max_characters=18_000)

    assert window == ["one", "two"]
    assert window_ready(catalog, document_ids)
