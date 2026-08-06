"""Text-ish formats. No dependencies, so this parser always exists."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from pathlib import Path

from bismuth.adapters.parsers.registry import build_extraction
from bismuth.domain.document import Extraction, Section

_ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "euc-kr", "cp1252", "latin-1")


def read_text_forgivingly(path: Path) -> str:
    """Decode a text file without knowing its encoding, trying ``cp949``/``euc-kr`` before Latin fallbacks (``latin-1`` decodes anything, so it goes last)."""
    data = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class PlainTextParser:
    """Plain text, Markdown, and other line-oriented formats."""

    @property
    def name(self) -> str:
        return "plain"

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".txt", ".md", ".markdown", ".rst", ".log", ".json", ".yaml", ".yml"})

    def warm(self) -> None:
        """Nothing to import: stdlib only."""

    def parse(self, path: Path, *, max_chars: int) -> Extraction:
        text = read_text_forgivingly(path)
        return build_extraction(_split_on_headings(text), parser=self.name, max_chars=max_chars)


class CsvParser:
    """Tabular text, rendered as Markdown rows so columns stay labelled for the model."""

    @property
    def name(self) -> str:
        return "csv"

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".csv", ".tsv"})

    def warm(self) -> None:
        """Nothing to import: stdlib only."""

    def parse(self, path: Path, *, max_chars: int) -> Extraction:
        text = read_text_forgivingly(path)
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        if not rows:
            return build_extraction([], parser=self.name, max_chars=max_chars)

        header, *body = rows
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
            *("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in body),
        ]
        section = Section(heading=path.stem, text="\n".join(lines), order=0)
        return build_extraction([section], parser=self.name, max_chars=max_chars)


def _split_on_headings(text: str) -> Iterator[Section]:
    """Split Markdown on ATX headings; yield one section if there are none."""
    lines = text.splitlines()
    heading: str | None = None
    buffer: list[str] = []
    order = 0

    for line in lines:
        if line.startswith("#") and " " in line[:8]:
            if buffer or heading:
                yield Section(heading=heading, text="\n".join(buffer).strip(), order=order)
                order += 1
                buffer = []
            heading = line.lstrip("#").strip()
        else:
            buffer.append(line)

    if buffer or heading:
        yield Section(heading=heading, text="\n".join(buffer).strip(), order=order)
