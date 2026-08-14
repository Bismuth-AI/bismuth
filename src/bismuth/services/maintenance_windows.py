"""Bounded arrival windows for incremental autonomous maintenance.

The count is a context ceiling, never a semantic trigger.  A window may close earlier
when compact cards are unusually verbose; folder decisions remain the model's job.
"""

from __future__ import annotations

from collections.abc import Sequence

from bismuth.domain.document import DocumentCard, SourceRef
from bismuth.ports.catalog import Catalog

MAX_WINDOW_DOCUMENTS = 30
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


def _family_text(value: object) -> str:
    return "".join(character.casefold() for character in str(value) if character.isalnum())


def _family_title(catalog: Catalog, document_id: str) -> str:
    """Use independently grounded card-title/source evidence for window cohesion."""

    card = catalog.load_card(document_id)
    source = catalog.load_source(document_id)
    if card is None or not isinstance(source, SourceRef):
        return ""
    title = _family_text(card.title)
    filename = _family_text(source.filename)
    return title if len(title) >= 4 and filename.startswith(title) else ""


def _same_family(left: str, right: str) -> bool:
    if not left or not right:
        return False
    shorter, longer = sorted((left, right), key=len)
    return shorter == longer or (
        len(shorter) >= 6 and longer.startswith(shorter) and len(shorter) / len(longer) >= 0.6
    )


def family_components(catalog: Catalog, document_ids: Sequence[str]) -> list[list[str]]:
    """Keep a grounded family indivisible while preserving first-seen queue priority."""

    ordered = list(dict.fromkeys(document_ids))
    titles = {document_id: _family_title(catalog, document_id) for document_id in ordered}
    parent = {document_id: document_id for document_id in ordered}

    def find(document_id: str) -> str:
        while parent[document_id] != document_id:
            parent[document_id] = parent[parent[document_id]]
            document_id = parent[document_id]
        return document_id

    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            if _same_family(titles[left], titles[right]):
                left_root, right_root = find(left), find(right)
                if left_root != right_root:
                    parent[right_root] = left_root

    groups: dict[str, list[str]] = {}
    for document_id in ordered:
        groups.setdefault(find(document_id), []).append(document_id)
    return list(groups.values())


def family_closure(catalog: Catalog, document_ids: Sequence[str]) -> list[str]:
    """Expand selected work with only its exact grounded family mates.

    The seed order remains authoritative.  A mate may come from an earlier window or a
    deferred checkpoint, but unrelated historical documents never enter the window.
    This gives the planner and the validator the same family boundary without replaying
    an entire failed backlog.
    """

    seeds = list(dict.fromkeys(document_ids))
    if not seeds:
        return []
    all_document_ids = [document_id for document_id, _ in catalog.iter_cards()]
    component_by_member: dict[str, list[str]] = {}
    for component in family_components(catalog, all_document_ids):
        for document_id in component:
            component_by_member[document_id] = component

    expanded: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        for document_id in component_by_member.get(seed, [seed]):
            if document_id in seen:
                continue
            expanded.append(document_id)
            seen.add(document_id)
    return expanded


def next_window(
    catalog: Catalog,
    document_ids: Sequence[str],
    *,
    max_documents: int = MAX_WINDOW_DOCUMENTS,
    max_characters: int = MAX_WINDOW_CHARACTERS,
) -> list[str]:
    """Pack a bounded window without separating grounded document families."""
    chosen: list[str] = []
    characters = 0
    for family in family_components(catalog, document_ids):
        family_weight = sum(card_characters(catalog.load_card(item)) for item in family)
        if len(family) > max_documents:
            # An oversized indivisible family cannot safely enter an ordinary window.
            continue
        if chosen and (
            len(chosen) + len(family) > max_documents or characters + family_weight > max_characters
        ):
            continue
        if not chosen and family_weight > max_characters:
            # The character ceiling is soft for one indivisible family; the hard
            # document-count ceiling and family invariant take precedence.
            chosen.extend(family)
            break
        chosen.extend(family)
        characters += family_weight
        if len(chosen) >= max_documents:
            break
    return chosen


def window_ready(catalog: Catalog, document_ids: Sequence[str]) -> bool:
    """Whether at least one bounded window is full and should run before the next file."""
    if not document_ids:
        return False
    window = next_window(catalog, document_ids)
    return (
        len(window) < len(document_ids)
        or len(window) >= MAX_WINDOW_DOCUMENTS
        or sum(card_characters(catalog.load_card(document_id)) for document_id in window)
        >= MAX_WINDOW_CHARACTERS
    )
