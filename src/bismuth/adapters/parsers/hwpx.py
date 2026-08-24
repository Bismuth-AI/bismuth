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
            names = sorted(
                (n for n in archive.namelist() if _SECTION_FILE.search(n)),
                key=lambda n: int(_SECTION_FILE.search(n).group(1)),  # type: ignore[union-attr]
            )
            if not names:
                raise ParserUnavailableError(
                    f"{path.name}: no Contents/section*.xml -- not an HWPX document"
                )
            sections, incomplete = _sections(archive, names)

        extraction = build_extraction(sections, parser=self.name, max_chars=max_chars)
        return extraction.model_copy(update={"truncated": True}) if incomplete else extraction


def _sections(archive: zipfile.ZipFile, names: list[str]) -> tuple[list[Section], bool]:
    sections: list[Section] = []
    incomplete = False
    for order, name in enumerate(names):
        try:
            root = ElementTree.fromstring(archive.read(name))
        except ElementTree.ParseError:
            incomplete = True
            continue
        text = _text_of(root).strip()
        if text:
            sections.append(Section(heading=None, text=text, order=order))
    return sections, incomplete


def _local(tag: str) -> str:
    """Strip the XML namespace from an element tag."""
    return tag.rpartition("}")[2]


def _text_of(element: ElementTree.Element) -> str:
    """Recover structured text in document order."""
    out: list[str] = []
    _walk(element, out)
    # Collapse the runs of blank lines that per-element emission produces.
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


def _walk(element: ElementTree.Element, out: list[str], *, in_cell: bool = False) -> None:
    tag = _local(element.tag)

    if tag == "t":  # a text run: the leaves
        out.append("".join(element.itertext()))
        return
    if tag in ("lineBreak", "linesegarray"):
        out.append("\n")
        return

    for child in element:
        _walk(child, out, in_cell=in_cell or tag == "tc")

    # Emit separators on the way out, so nested structures close inside-out.
    if tag == "p":
        # A paragraph inside a table cell must not end the line, or each cell
        # would land on its own row.
        out.append(" " if in_cell else "\n")
    elif tag == "tc":  # table cell
        out.append("\t")
    elif tag == "tr" or tag == "tbl":  # table row
        out.append("\n")
