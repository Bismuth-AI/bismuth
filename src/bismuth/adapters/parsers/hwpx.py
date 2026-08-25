"""HWPX text extraction from zipped XML."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from bismuth.adapters.parsers.registry import build_extraction
from bismuth.domain.document import Extraction, Section
from bismuth.domain.errors import ParserUnavailableError

#: Sorted numerically, not lexicographically, so section10 doesn't come before section9.
_SECTION_FILE = re.compile(r"Contents/section(\d+)\.xml$", re.IGNORECASE)


class HwpxParser:
    """Read Hancom HWPX documents."""

    @property
    def name(self) -> str:
        return "hwpx"

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".hwpx"})

    def warm(self) -> None:
        """Nothing to import: HWPX is a zip of XML, both stdlib."""

    def parse(self, path: Path, *, max_chars: int) -> Extraction:
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            hint = ""
            if path.suffix.lower() == ".hwp":
                hint = " (legacy .hwp is unsupported; re-save it as .hwpx in Hancom Hangul)"
            raise ParserUnavailableError(
                f"{path.name} is not a readable HWPX file{hint}: {exc}"
            ) from exc

        with archive:
            _validate_archive(archive)
            names = sorted(
                (n for n in archive.namelist() if _SECTION_FILE.search(n)),
                key=lambda n: int(_SECTION_FILE.search(n).group(1)),  # type: ignore[union-attr]
            )
            if not names:
                raise ParserUnavailableError(
                    f"{path.name}: no Contents/section*.xml -- not an HWPX document"
                )
            sections, incomplete = _sections(archive, names)

        if not sections:
            raise ParserUnavailableError(f"{path.name} has no extractable text")
        extraction = build_extraction(sections, parser=self.name, max_chars=max_chars)
        return extraction.model_copy(update={"truncated": True}) if incomplete else extraction


def _sections(archive: zipfile.ZipFile, names: list[str]) -> tuple[list[Section], bool]:
    sections: list[Section] = []
    incomplete = False
    for order, name in enumerate(names):
        try:
            root = ElementTree.fromstring(archive.read(name))
        except (ElementTree.ParseError, zipfile.BadZipFile, OSError, RuntimeError):
            incomplete = True
            continue
        text = _text_of(root).strip()
        if text:
            sections.append(Section(heading=None, text=text, order=order))
    return sections, incomplete


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > 10_000 or sum(info.file_size for info in infos) > 500 * 1024 * 1024:
        raise ParserUnavailableError("HWPX archive is too large after decompression")
    if "META-INF/manifest.xml" in archive.namelist():
        try:
            manifest = ElementTree.fromstring(archive.read("META-INF/manifest.xml"))
        except ElementTree.ParseError:
            return
        if any("encryption-data" in element.tag for element in manifest.iter()):
            raise ParserUnavailableError("HWPX document is password-protected")


def _local(tag: str) -> str:
    """Strip the XML namespace from an element tag."""
    return tag.rpartition("}")[2]


def _text_of(element: ElementTree.Element) -> str:
    """Recover paragraphs and Markdown tables in document order."""
    tokens: list[str] = []
    _walk(element, tokens)
    return "\n\n".join(token for token in tokens if token.strip())


def _walk(element: ElementTree.Element, out: list[str]) -> None:
    tag = _local(element.tag)

    if tag == "tbl":
        if table := _table_to_markdown(element):
            out.append(table)
        return
    if tag == "p":
        if any(_local(node.tag) == "tbl" for node in element.iter() if node is not element):
            for child in element:
                _walk(child, out)
        elif text := _collect_text(element).strip():
            out.append(text)
        return
    for child in element:
        _walk(child, out)


def _collect_text(element: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        tag = _local(node.tag)
        if tag == "t" and node.text:
            parts.append(node.text)
        elif tag in {"lineBreak", "br"}:
            parts.append("\n")
    return "".join(parts)


def _integer_attribute(element: ElementTree.Element, name: str, default: int) -> int:
    for key, value in element.attrib.items():
        if key.rpartition("}")[2] == name:
            try:
                return int(value)
            except ValueError:
                return default
    return default


def _table_to_markdown(table: ElementTree.Element) -> str:
    cells: list[tuple[int, int, int, int, str]] = []
    fallback_row = 0
    for row_element in table:
        if _local(row_element.tag) != "tr":
            continue
        fallback_column = 0
        for cell in row_element:
            if _local(cell.tag) != "tc":
                continue
            row, column, row_span, column_span = fallback_row, fallback_column, 1, 1
            for child in cell:
                tag = _local(child.tag)
                if tag == "cellAddr":
                    row = _integer_attribute(child, "rowAddr", row)
                    column = _integer_attribute(child, "colAddr", column)
                elif tag == "cellSpan":
                    row_span = _integer_attribute(child, "rowSpan", 1)
                    column_span = _integer_attribute(child, "colSpan", 1)
            text = " ".join(_collect_text(cell).split()).replace("|", "\\|")
            cells.append((row, column, row_span, column_span, text))
            fallback_column += column_span
        fallback_row += 1
    if not cells:
        return ""

    height = max(row + row_span for row, _, row_span, _, _ in cells)
    width = max(column + column_span for _, column, _, column_span, _ in cells)
    grid = [["" for _ in range(width)] for _ in range(height)]
    for row, column, _, _, text in cells:
        grid[row][column] = text
    lines = [
        "| " + " | ".join(grid[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in grid[1:])
    return "\n".join(lines)
