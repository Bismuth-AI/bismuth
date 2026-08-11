"""Sanitizes a model-chosen name into a filesystem-safe path segment."""

from __future__ import annotations

import re
import unicodedata

#: Characters no mainstream filesystem accepts; Windows' stricter set is applied everywhere.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Names Windows reserves regardless of extension.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_MAX_SEGMENT = 64


def sanitize_segment(raw: str) -> str:
    """Turn one path segment the model proposed into something safe everywhere.

    Raises:
        ValueError: if nothing usable survives.
    """
    text = unicodedata.normalize("NFC", raw).strip()
    text = _ILLEGAL.sub(" ", text)
    text = " ".join(text.split())
    # Windows drops trailing dots/spaces, so strip them to avoid a collision.
    text = text.rstrip(". ")

    if len(text) > _MAX_SEGMENT:
        text = text[:_MAX_SEGMENT].rstrip(". ")

    if not text:
        raise ValueError(f"{raw!r} leaves nothing usable as a folder name")
    if text.casefold() in _WINDOWS_RESERVED:
        text = f"{text}_"
    if text.startswith(("_", ".")):
        # Leading underscore and dot names are Bismuth/private namespaces
        # (_inbox, _folder.md, .bismuth). Model-authored classes never target them.
        text = text.lstrip("._") or "unnamed"
    return text
