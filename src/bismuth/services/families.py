"""Language-neutral document-family identity grounded in source filenames."""

from __future__ import annotations

from bismuth.domain.document import DocumentCard


def family_text(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def grounded_family_keys(card: DocumentCard, filename: str) -> set[str]:
    """Return title/topic identities visibly anchored at the filename's start."""

    source_key = family_text(filename)
    candidates = [card.title, *card.topics]
    return {
        key
        for value in candidates
        if len(key := family_text(value)) >= 6 and source_key.startswith(key)
    }


def same_family_key(left: str, right: str) -> bool:
    if not left or not right:
        return False
    shorter, longer = sorted((left, right), key=len)
    return shorter == longer or (
        len(shorter) >= 6 and longer.startswith(shorter) and len(shorter) / len(longer) >= 0.6
    )


def key_sets_overlap(left: set[str], right: set[str]) -> bool:
    return any(same_family_key(a, b) for a in left for b in right)
