"""Direct HWP 5.x extraction based on Memento's OLE parser."""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from pathlib import Path

from bismuth.adapters.parsers.registry import build_extraction, require
from bismuth.domain.document import Extraction, Section
from bismuth.domain.errors import ParserUnavailableError

FILE_HEADER = "FileHeader"
BODY_SECTION = "BodyText/Section{}"
TAG_PARA_TEXT = 67
TAG_LIST_HEADER = 72
TAG_TABLE = 77


class HwpParser:
    """Read HWP 5.x compound documents without converting them to HWPX."""

    @property
    def name(self) -> str:
        return "hwp5"

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".hwp"})

    def warm(self) -> None:
        require("olefile", "Reading .hwp needs olefile: pip install 'bismuth-kb[parsers]'")

    def parse(self, path: Path, *, max_chars: int) -> Extraction:
        self.warm()
        import olefile

        if not olefile.isOleFile(str(path)):
            raise ParserUnavailableError(f"{path.name} is not an HWP 5.x OLE document")

        try:
            with olefile.OleFileIO(str(path)) as ole:
                if not ole.exists(FILE_HEADER):
                    raise ParserUnavailableError(f"{path.name}: missing HWP FileHeader stream")
                header = ole.openstream(FILE_HEADER).read(256)
                if len(header) < 40:
                    raise ParserUnavailableError(f"{path.name}: damaged HWP FileHeader")
                properties = struct.unpack_from("<I", header, 36)[0]
                if properties & 0x02:
                    raise ParserUnavailableError(f"{path.name} is password-protected")

                sections = list(_ole_sections(ole))
        except ParserUnavailableError:
            raise
        except (OSError, struct.error) as exc:
            raise ParserUnavailableError(f"{path.name} is not a readable .hwp: {exc}") from exc

        if not sections:
            raise ParserUnavailableError(f"{path.name} has no extractable text")
        return build_extraction(sections, parser=self.name, max_chars=max_chars)


def _ole_sections(ole: object) -> Iterator[Section]:
    index = 0
    while ole.exists(BODY_SECTION.format(index)):  # type: ignore[attr-defined]
        raw = ole.openstream(BODY_SECTION.format(index)).read()  # type: ignore[attr-defined]
        data = _decompress(raw)
        text = _process_section(data).strip()
        if text:
            yield Section(heading=None, text=text, order=index)
        index += 1


def _decompress(data: bytes) -> bytes:
    for window_bits in (15, -15):
        try:
            return zlib.decompress(data, window_bits)
        except zlib.error:
            pass
    return data


def _parse_records(data: bytes) -> list[tuple[int, int, bytes]]:
    records: list[tuple[int, int, bytes]] = []
    offset = 0
    while offset + 4 <= len(data):
        header = struct.unpack_from("<I", data, offset)[0]
        tag_id = header & 0x3FF
        level = (header >> 10) & 0x3FF
        size = (header >> 20) & 0xFFF
        offset += 4
        if size == 0xFFF:
            if offset + 4 > len(data):
                break
            size = struct.unpack_from("<I", data, offset)[0]
            offset += 4
        if offset + size > len(data):
            break
        if size:
            records.append((tag_id, level, data[offset : offset + size]))
        offset += size
    return records


def _extract_para_text(data: bytes) -> str:
    # HWP stores paragraph text as UTF-16LE code units interspersed with controls.
    clean = bytearray()
    offset = 0
    while offset + 2 <= len(data):
        code = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        if code in (10, 13):
            clean.extend("\n".encode("utf-16le"))
        elif code == 9:
            clean.extend("\t".encode("utf-16le"))
            offset += 14
        elif 1 <= code <= 23:
            # Inline/extended controls occupy eight UTF-16 code units: the
            # opening code, a six-unit payload such as ``secd``, and a closing code.
            offset += 14
        elif code in {24, 31}:
            clean.extend(" ".encode("utf-16le"))
        elif code == 30:
            clean.extend("-".encode("utf-16le"))
        elif code >= 32:
            clean.extend(struct.pack("<H", code))
    return clean.decode("utf-16le", errors="replace")


def _table_markdown(records: list[tuple[int, int, bytes]], start: int) -> tuple[str, int]:
    _, table_level, table_data = records[start]
    if len(table_data) < 8:
        return "", start + 1
    rows, columns = struct.unpack_from("<HH", table_data, 4)
    if not rows or not columns:
        return "", start + 1

    cells: dict[tuple[int, int], str] = {}
    current: tuple[int, int] | None = None
    cell_text: list[str] = []
    sequence = 0
    index = start + 1
    while index < len(records):
        tag_id, level, data = records[index]
        if level < table_level:
            break
        if tag_id == TAG_LIST_HEADER and level == table_level:
            if current is not None:
                cells[current] = " ".join(cell_text).strip()
            cell_text = []
            if len(data) >= 12:
                column, row = struct.unpack_from("<HH", data, 8)
                current = (row, column)
            else:
                current = (sequence // columns, sequence % columns)
            sequence += 1
        elif tag_id == TAG_PARA_TEXT and current is not None:
            if text := _extract_para_text(data).strip():
                cell_text.append(text)
        index += 1
    if current is not None:
        cells[current] = " ".join(cell_text).strip()
    if not cells:
        return "", index

    height = max(rows, max(row for row, _ in cells) + 1)
    width = max(columns, max(column for _, column in cells) + 1)
    grid = [["" for _ in range(width)] for _ in range(height)]
    for (row, column), text in cells.items():
        if row < height and column < width:
            grid[row][column] = text.replace("|", "\\|")
    return _markdown_table(grid), index


def _markdown_table(rows: list[list[str]]) -> str:
    lines: list[str] = []
    for index, row in enumerate(rows):
        lines.append("| " + " | ".join(row) + " |")
        if index == 0:
            lines.append("| " + " | ".join("---" for _ in row) + " |")
    return "\n".join(lines)


def _process_section(data: bytes) -> str:
    records = _parse_records(data)
    blocks: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        tag_id, _, record_data = records[index]
        if tag_id == TAG_TABLE:
            table, index = _table_markdown(records, index)
            if table:
                blocks.append(("table", table))
            continue
        if tag_id == TAG_PARA_TEXT and (text := _extract_para_text(record_data).strip()):
            blocks.append(("text", text))
        index += 1
    return "\n\n".join(content for _, content in blocks)
