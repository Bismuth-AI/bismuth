"""Domain layer: value objects and pure functions, no I/O."""

from bismuth.domain.charter import Charter
from bismuth.domain.document import (
    DocumentCard,
    Entity,
    EntityKind,
    Extraction,
    Section,
    SourceRef,
    sidecar_name,
)
from bismuth.domain.errors import (
    BismuthError,
    CharterError,
    JournalCorruptError,
    ParserUnavailableError,
    StructuredOutputError,
    VaultError,
)
from bismuth.domain.journal import (
    EntryStatus,
    JournalEntry,
    Operation,
    OperationKind,
)
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.placement import Placement, Verdict

__all__ = [
    "BismuthError",
    "Charter",
    "CharterError",
    "DocumentCard",
    "Entity",
    "EntityKind",
    "EntryStatus",
    "Extraction",
    "JournalCorruptError",
    "JournalEntry",
    "Operation",
    "OperationKind",
    "ParserUnavailableError",
    "Placement",
    "Section",
    "SourceRef",
    "StructuredOutputError",
    "VaultError",
    "Verdict",
    "sanitize_segment",
    "sidecar_name",
]
