"""Bounded arrival windows for incremental autonomous maintenance.

The count is a context ceiling, never a semantic trigger.  A window may close earlier
when compact cards are unusually verbose; folder decisions remain the model's job.
"""

from __future__ import annotations

from collections.abc import Sequence

from bismuth.domain.document import DocumentCard
from bismuth.ports.catalog import Catalog

MAX_WINDOW_DOCUMENTS = 50
MAX_WINDOW_CHARACTERS = 18_000


def card_characters(card: DocumentCard | None) -> int:
    """Approximate the compact evidence sent to the maintenance agent."""
    if card is None:
        return 1
    return sum(
        len(value)
        for value in (
            card.title,
            card.summary,
            card.doc_type,
            *card.topics,
            *card.keywords,
        )
    )


def next_window(
    catalog: Catalog,
    document_ids: Sequence[str],
    *,
    max_documents: int = MAX_WINDOW_DOCUMENTS,
    max_characters: int = MAX_WINDOW_CHARACTERS,
) -> list[str]:
    """Take one non-empty prefix that fits both execution ceilings."""
    chosen: list[str] = []
    characters = 0
    for document_id in document_ids:
        weight = card_characters(catalog.load_card(document_id))
        if chosen and (len(chosen) >= max_documents or characters + weight > max_characters):
            break
        chosen.append(document_id)
        characters += weight
    return chosen


def window_ready(catalog: Catalog, document_ids: Sequence[str]) -> bool:
    """Whether at least one bounded window is full and should run before the next file."""
    if not document_ids:
        return False
    window = next_window(catalog, document_ids)
    return len(window) < len(document_ids) or len(window) >= MAX_WINDOW_DOCUMENTS or sum(
        card_characters(catalog.load_card(document_id)) for document_id in window
    ) >= MAX_WINDOW_CHARACTERS
