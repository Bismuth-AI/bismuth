"""Direct OOXML Word parser based on Memento's structured DOCX parser."""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from bismuth.adapters.parsers.registry import build_extraction, require
from bismuth.domain.document import Extraction, Section
from bismuth.domain.errors import ParserUnavailableError

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
V = "{urn:schemas-microsoft-com:vml}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class DocxParser:
    """Read DOCX XML directly, preserving paragraph/table order and annotations."""

    @property
    def name(self) -> str:
        return "docx-structured"

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".docx"})

    def warm(self) -> None:
        require("lxml", "Reading .docx needs lxml: pip install 'bismuth-kb[parsers]'")

    def parse(self, path: Path, *, max_chars: int) -> Extraction:
        self.warm()
        try:
            with zipfile.ZipFile(path) as archive:
                _validate_archive(archive)
                names = set(archive.namelist())
                if "word/document.xml" not in names:
                    raise ParserUnavailableError(
                        f"{path.name}: missing word/document.xml -- not a DOCX document"
                    )
                relationships = (
                    _relationships(archive.read("word/_rels/document.xml.rels"))
                    if "word/_rels/document.xml.rels" in names
                    else {}
                )
                comments = (
                    _comments(archive.read("word/comments.xml"))
                    if "word/comments.xml" in names
                    else {}
                )
                sections = list(_blocks(archive.read("word/document.xml"), relationships, comments))
        except ParserUnavailableError:
            raise
        except (zipfile.BadZipFile, OSError, KeyError, RuntimeError, ValueError) as exc:
            raise ParserUnavailableError(f"{path.name} is not a readable .docx: {exc}") from exc

        if not sections:
            raise ParserUnavailableError(f"{path.name} has no extractable text")
        return build_extraction(sections, parser=self.name, max_chars=max_chars)


def _xml(data: bytes) -> object:
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    return etree.fromstring(data, parser=parser)


def _local(element: object) -> str:
    return str(element.tag).rpartition("}")[2]  # type: ignore[attr-defined]


def _paragraph_text(paragraph: object) -> str:
    parts: list[str] = []
    for child in paragraph.iter():  # type: ignore[attr-defined]
        tag = _local(child)
        if tag in {"t", "instrText"}:
            parts.append(child.text or "")
        elif tag == "tab":
            parts.append("\t")
        elif tag in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts)


def _relationships(data: bytes) -> dict[str, str]:
    relationships: dict[str, str] = {}
    root = _xml(data)
    for relationship in root.iter(PR + "Relationship"):  # type: ignore[attr-defined]
        rid, target = relationship.get("Id"), relationship.get("Target")
        if rid and target and not target.startswith(("http:", "https:")):
            normalized = PurePosixPath("word", target).as_posix()
            relationships[rid] = normalized
    return relationships


def _comments(data: bytes) -> dict[str, str]:
    comments: dict[str, str] = {}
    root = _xml(data)
    for comment in root.iter(W + "comment"):  # type: ignore[attr-defined]
        comment_id = comment.get(W + "id")
        if comment_id is not None:
            comments[comment_id] = "".join(
                node.text or "" for node in comment.iter(W + "t")
            ).strip()
    return comments


def _comment_ids(paragraph: object) -> list[str]:
    return [
        ref.get(W + "id")
        for ref in paragraph.iter(W + "commentReference")  # type: ignore[attr-defined]
        if ref.get(W + "id") is not None
    ]


def _image_names(paragraph: object, relationships: dict[str, str]) -> list[str]:
    ids: list[str] = []
    for element in paragraph.iter(A + "blip"):  # type: ignore[attr-defined]
        if rid := element.get(R + "embed") or element.get(R + "link"):
            ids.append(rid)
    for element in paragraph.iter(V + "imagedata"):  # type: ignore[attr-defined]
        if rid := element.get(R + "id") or element.get(R + "link"):
            ids.append(rid)
    return [Path(relationships[rid]).name for rid in ids if rid in relationships]


def _paragraph_block(
    paragraph: object, relationships: dict[str, str], comments: dict[str, str]
) -> str:
    parts: list[str] = []
    if text := _paragraph_text(paragraph).strip():
        parts.append(text)
    for comment_id in _comment_ids(paragraph):
        if body := comments.get(comment_id, "").strip():
            parts.append(f"[메모 #{comment_id}: {body}]")
    parts.extend(f"[이미지: {name}]" for name in _image_names(paragraph, relationships))
    return "\n".join(parts)


def _table(table: object, relationships: dict[str, str], comments: dict[str, str]) -> str:
    rows: list[list[str]] = []
    for row in table.findall(W + "tr"):  # type: ignore[attr-defined]
        cells: list[str] = []
        for cell in row.findall(W + "tc"):
            paragraphs = [
                _paragraph_block(p, relationships, comments) for p in cell.findall(W + "p")
            ]
            cells.append(" / ".join(filter(None, paragraphs)).replace("|", "\\|"))
        rows.append(cells)
    if not rows:
        return ""
    width = max(map(len, rows))
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def _blocks(
    document_xml: bytes, relationships: dict[str, str], comments: dict[str, str]
) -> Iterator[Section]:
    root = _xml(document_xml)
    body = root.find(W + "body")  # type: ignore[attr-defined]
    if body is None:
        return
    order = 0
    for child in body:
        tag = _local(child)
        heading = None
        text = _paragraph_block(child, relationships, comments) if tag == "p" else ""
        if tag == "tbl":
            text = _table(child, relationships, comments)
            heading = "Table"
        if text:
            yield Section(heading=heading, text=text, order=order)
            order += 1


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > 10_000 or sum(info.file_size for info in infos) > 500 * 1024 * 1024:
        raise ParserUnavailableError("DOCX archive is too large after decompression")
