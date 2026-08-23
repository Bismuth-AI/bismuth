"""Documents: the source file, its extracted text, and the model's card about it."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def sidecar_name(document_filename: str) -> str:
    """The sidecar for a document: ``contract.pdf`` -> ``contract.pdf.md``."""
    return f"{document_filename}.md"


LABEL_MAX_CHARS = 40
"""Maximum length of a topic or keyword used as a filing label."""

NAME_MAX_CHARS = 60
"""How long an entity name may be. Longer than a label because organisations have long
legal names, short enough that a pasted author list is not one."""

QUESTION_MAX_CHARS = 200
"""How long a question may be. A sentence, not a paragraph."""


class EntityKind(StrEnum):
    """The entity types Bismuth extracts."""

    ORGANIZATION = "organization"
    PERSON = "person"
    PROJECT = "project"
    PRODUCT = "product"
    LOCATION = "location"
    DATE = "date"


class Entity(BaseModel):
    """A named thing mentioned by a document."""

    model_config = ConfigDict(frozen=True)

    name: NonEmptyStr = Field(description="Canonical surface form, as written in the document.")
    kind: EntityKind

    def key(self) -> str:
        """A normalised identity for deduplication across documents."""
        return f"{self.kind.value}:{' '.join(self.name.casefold().split())}"


class Window(BaseModel):
    """A contiguous slice of a document's text, sized to fit one model call."""

    model_config = ConfigDict(frozen=True)

    index: int = Field(description="0-based position in reading order.")
    total: int = Field(description="How many windows the document was cut into.")
    start: int
    end: int
    text: str

    @property
    def label(self) -> str:
        """``3/7``, for prompts and logs."""
        return f"{self.index + 1}/{self.total}"


class Section(BaseModel):
    """A contiguous piece of a document, with enough anchoring to cite it."""

    model_config = ConfigDict(frozen=True)

    heading: str | None = None
    text: str
    page: int | None = Field(
        default=None, description="1-based page number, when the format has pages."
    )
    order: int = Field(description="Position within the document, 0-based.")


class SourceRef(BaseModel):
    """A pointer to the original file, plus enough to detect that it changed."""

    model_config = ConfigDict(frozen=True)

    path: Path = Field(description="Absolute path at the time of ingestion.")
    filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    modified_at: datetime

    @property
    def document_id(self) -> str:
        """Short, stable, human-quotable id. Collisions are not a concern at vault scale."""
        return self.sha256[:16]

    @property
    def extension(self) -> str:
        return self.path.suffix.lower()

    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


class Extraction(BaseModel):
    """Text recovered from a source file by a parser."""

    model_config = ConfigDict(frozen=True)

    sections: tuple[Section, ...]
    parser: str = Field(
        description="Name of the parser that produced this, for debugging bad output."
    )
    page_count: int | None = None
    truncated: bool = Field(
        default=False,
        description="True when the source was longer than the extraction budget.",
    )

    @property
    def text(self) -> str:
        """The whole document as one string, headings included."""
        parts: list[str] = []
        for section in self.sections:
            if section.heading:
                parts.append(f"\n## {section.heading}\n")
            parts.append(section.text)
        return "\n".join(parts).strip()

    def windows(self, size: int) -> tuple[Window, ...]:
        """Cut the whole text into sequential windows of at most ``size`` characters.

        Order and length are the only structure every document has -- headings,
        pages and paragraphs are all optional -- so the cut is by length, and merely
        snaps back to a nearby line break when one happens to be there.
        """
        text = self.text
        if not text:
            return ()

        slack = max(size // 8, 1)
        bounds: list[tuple[int, int]] = []
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                snap = text.rfind("\n", end - slack, end)
                if snap < 0:
                    snap = text.rfind(" ", end - slack, end)
                if snap > start:
                    end = snap + 1
            bounds.append((start, end))
            start = end

        return tuple(
            Window(index=i, total=len(bounds), start=s, end=e, text=text[s:e])
            for i, (s, e) in enumerate(bounds)
        )


class Coverage(BaseModel):
    """How much of a document reached the model and contributed to its card."""

    model_config = ConfigDict(frozen=True)

    chars_total: int = Field(ge=0, description="Characters the parser produced.")
    chars_read: int = Field(ge=0, description="Characters actually sent to the model.")
    windows_total: int = Field(ge=0)
    windows_read: int = Field(ge=0)
    windows_contributed: int = Field(
        default=0, ge=0, description="Windows that added at least one fact the card lacked."
    )
    windows_failed: int = Field(default=0, ge=0, description="Windows the model choked on.")
    extraction_truncated: bool = Field(
        default=False, description="The parser itself hit its budget before the file ended."
    )

    @property
    def read_ratio(self) -> float:
        return self.chars_read / self.chars_total if self.chars_total else 1.0

    @property
    def whole_document(self) -> bool:
        """True when every extracted character was read and nothing was cut upstream."""
        return not self.extraction_truncated and self.windows_read >= self.windows_total


class DocumentCard(BaseModel):
    """What a model concluded about a document."""

    model_config = ConfigDict(frozen=True)

    title: NonEmptyStr = Field(
        description="The document's own title, taken from its content rather than its filename."
    )
    summary: NonEmptyStr = Field(
        description="Two or three sentences. What it is, not how it reads."
    )
    doc_type: NonEmptyStr = Field(
        description="A noun for the genre: contract, proposal, meeting notes, invoice, spec."
    )
    topics: tuple[str, ...] = Field(
        default=(),
        description=(
            "The handful of things this document is *about* -- a project name, a "
            "client, a subject. What a person would say if asked which drawer it "
            "belongs in. This is the raw material the model files by, and it is "
            "open-ended on purpose: no fixed set of categories, just what is there."
        ),
    )
    entities: tuple[Entity, ...] = ()
    keywords: tuple[str, ...] = ()
    language: str = Field(default="unknown", description="BCP-47-ish tag, best effort.")
    answers_questions: tuple[str, ...] = Field(
        default=(),
        description=(
            "Questions this document can answer. Written for an agent deciding "
            "whether to open the file -- the retrieval surface, not a summary."
        ),
    )
    coverage: Coverage | None = Field(
        default=None,
        description=(
            "How much of the document this card was built from. Optional so cards "
            "written before coverage existed still load."
        ),
    )

    def entity_keys(self) -> frozenset[str]:
        return frozenset(entity.key() for entity in self.entities)
