"""Folder maintenance: what may be asked of a folder, and what happened when it was.

The four operators of ADR-0018 are what this service is, and each has its own module:
:mod:`emerging` draws one class out of a pile or routes a loose document behind a sign
that already stands, :mod:`grouping` stands existing folders together under one broader
name, :mod:`splitting` dissolves a level that does not earn the guess it costs. What they
share -- reading a folder as cards, and the mechanical checks a proposal is held to before
any model sees it -- lives in :mod:`reading` and :mod:`naming`.

They are mixins on one class rather than free functions because every one of them needs
the same five collaborators and the same memories of what has already been refused here.
"""

from bismuth.services.subdivision.reading import (
    MAX_MAINTENANCE_PROMPT_CHARS,
    Divided,
)
from bismuth.services.subdivision.service import (
    LibraryMaintenanceService,
    SubdivisionService,
)

__all__ = [
    "MAX_MAINTENANCE_PROMPT_CHARS",
    "Divided",
    "LibraryMaintenanceService",
    "SubdivisionService",
]
