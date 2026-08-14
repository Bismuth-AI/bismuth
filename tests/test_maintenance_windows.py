"""Incremental maintenance is bounded by execution context, not corpus semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import Mock

from bismuth.domain.document import DocumentCard, SourceRef
from bismuth.ports.catalog import Catalog
from bismuth.services.maintenance_windows import family_closure, next_window, window_ready


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


def test_one_window_never_contains_more_than_thirty_arrivals() -> None:
    catalog = _catalog(_card())
    document_ids = [f"doc-{index}" for index in range(80)]

    window = next_window(catalog, document_ids)

    assert len(window) == 30
    assert window_ready(catalog, document_ids)


def test_twenty_nine_compact_arrivals_do_not_trigger_mid_upload() -> None:
    catalog = _catalog(_card())
    document_ids = [f"doc-{index}" for index in range(29)]

    assert len(next_window(catalog, document_ids)) == 29
    assert not window_ready(catalog, document_ids)


def test_thirtieth_compact_arrival_triggers_maintenance() -> None:
    catalog = _catalog(_card())
    document_ids = [f"doc-{index}" for index in range(30)]

    assert window_ready(catalog, document_ids)


def test_one_hundred_fifty_three_arrivals_split_into_five_full_windows_and_tail() -> None:
    catalog = _catalog(_card())
    remaining = [f"doc-{index}" for index in range(153)]
    window_sizes: list[int] = []

    while remaining:
        window = next_window(catalog, remaining)
        window_sizes.append(len(window))
        remaining = remaining[len(window) :]

    assert window_sizes == [30, 30, 30, 30, 30, 3]


def test_verbose_cards_close_a_window_before_the_count_limit() -> None:
    catalog = _catalog(_card("x" * 7_000))
    document_ids = ["one", "two", "three", "four"]

    window = next_window(catalog, document_ids, max_characters=18_000)

    assert window == ["one", "two"]
    assert window_ready(catalog, document_ids)


def test_window_keeps_a_family_together_even_when_its_mate_is_late_in_queue() -> None:
    ids = ["act", *(f"other-{index}" for index in range(29)), "decree"]
    cards = {document_id: _card() for document_id in ids}
    cards["act"] = _card().model_copy(update={"title": "과학관설립운영법"})
    cards["decree"] = _card().model_copy(update={"title": "과학관설립운영법 시행령"})
    sources = {
        document_id: SourceRef(
            path=Path(f"C:/{document_id}.txt"),
            filename=(
                "과학관설립운영법.txt"
                if document_id == "act"
                else "과학관설립운영법 시행령.txt"
                if document_id == "decree"
                else f"{document_id}.txt"
            ),
            size_bytes=1,
            sha256=f"{index:064x}",
            modified_at=datetime.now(UTC),
        )
        for index, document_id in enumerate(ids, start=1)
    }
    catalog = Mock()
    catalog.load_card.side_effect = cards.get
    catalog.load_source.side_effect = sources.get

    window = next_window(cast(Catalog, catalog), ids)

    assert len(window) == 30
    assert "act" in window
    assert "decree" in window


def test_family_closure_adds_only_grounded_mates_from_prior_state() -> None:
    ids = ["act", "decree", "unrelated"]
    cards = {document_id: _card() for document_id in ids}
    cards["act"] = _card().model_copy(update={"title": "과학관설립운영법"})
    cards["decree"] = _card().model_copy(update={"title": "과학관설립운영법 시행령"})
    sources = {
        "act": SourceRef(
            path=Path("C:/act.txt"),
            filename="과학관설립운영법.txt",
            size_bytes=1,
            sha256="1" * 64,
            modified_at=datetime.now(UTC),
        ),
        "decree": SourceRef(
            path=Path("C:/decree.txt"),
            filename="과학관설립운영법 시행령.txt",
            size_bytes=1,
            sha256="2" * 64,
            modified_at=datetime.now(UTC),
        ),
        "unrelated": SourceRef(
            path=Path("C:/unrelated.txt"),
            filename="unrelated.txt",
            size_bytes=1,
            sha256="3" * 64,
            modified_at=datetime.now(UTC),
        ),
    }
    catalog = Mock()
    catalog.iter_cards.return_value = iter((document_id, cards[document_id]) for document_id in ids)
    catalog.load_card.side_effect = cards.get
    catalog.load_source.side_effect = sources.get

    assert family_closure(cast(Catalog, catalog), ["decree"]) == ["act", "decree"]
