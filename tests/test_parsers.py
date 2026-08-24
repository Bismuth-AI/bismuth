"""Document parsers, primarily the custom HWPX parser."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from bismuth.adapters.parsers import (
    CsvParser,
    HwpxParser,
    PdfParser,
    PlainTextParser,
    build_registry,
)
from bismuth.adapters.parsers.office import _sheets
from bismuth.adapters.parsers.pdf import _pages
from bismuth.domain.errors import ParserUnavailableError

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"


def make_hwpx(tmp_path: Path, section_xml: str, *, name: str = "doc.hwpx") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("version.xml", "<version/>")
        archive.writestr("Contents/section0.xml", section_xml)
    return path


def paragraph(*runs: str) -> str:
    inner = "".join(f"<hp:run><hp:t>{text}</hp:t></hp:run>" for text in runs)
    return f"<hp:p>{inner}</hp:p>"


def make_pdf(tmp_path: Path, *pages: str, name: str = "document.pdf") -> Path:
    path = tmp_path / name
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}  # type: ignore[attr-defined]
                )
            }
        )
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)  # type: ignore[attr-defined]
    with path.open("wb") as output:
        writer.write(output)
    return path


class TestPdf:
    def test_extracts_text_with_page_numbers(self, tmp_path: Path) -> None:
        extraction = PdfParser().parse(
            make_pdf(tmp_path, "Alpha project report", "Beta research notes"), max_chars=10_000
        )

        assert "Alpha project report" in extraction.text
        assert "Beta research notes" in extraction.text
        assert [section.page for section in extraction.sections] == [1, 2]
        assert extraction.page_count == 2
        assert extraction.parser == "pypdf"

    def test_respects_the_extraction_limit(self, tmp_path: Path) -> None:
        extraction = PdfParser().parse(make_pdf(tmp_path, "x" * 500), max_chars=100)

        assert extraction.truncated
        assert len(extraction.text) <= 100

    def test_rejects_a_pdf_without_extractable_text(self, tmp_path: Path) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        path = tmp_path / "scan.pdf"
        with path.open("wb") as output:
            writer.write(output)

        with pytest.raises(ParserUnavailableError, match="no extractable text"):
            PdfParser().parse(path, max_chars=10_000)

    def test_rejects_a_damaged_pdf(self, tmp_path: Path) -> None:
        path = tmp_path / "damaged.pdf"
        path.write_bytes(b"not a pdf")

        with pytest.raises(ParserUnavailableError, match="not a readable PDF"):
            PdfParser().parse(path, max_chars=10_000)

    def test_one_unreadable_page_marks_the_extraction_incomplete(self) -> None:
        class Page:
            def __init__(self, text: str | None = None) -> None:
                self.text = text

            def extract_text(self) -> str:
                if self.text is None:
                    raise ValueError("unreadable")
                return self.text

        class Reader:
            def __init__(self) -> None:
                self.pages = [Page("first"), Page(), Page("third")]

        sections, incomplete = _pages(Reader())

        assert [section.text for section in sections] == ["first", "third"]
        assert [section.page for section in sections] == [1, 3]
        assert incomplete


class TestHwpx:
    """HWPX parsing."""

    def test_reads_korean_paragraphs(self, tmp_path: Path) -> None:
        path = make_hwpx(
            tmp_path,
            f'<hp:sec xmlns:hp="{HP}">{paragraph("아폴로 사업 계약서")}{paragraph("계약 기간은 24개월로 한다.")}</hp:sec>',
        )
        extraction = HwpxParser().parse(path, max_chars=10_000)

        assert "아폴로 사업 계약서" in extraction.text
        assert "계약 기간은 24개월로 한다." in extraction.text
        assert extraction.parser == "hwpx"

    def test_joins_runs_within_a_paragraph(self, tmp_path: Path) -> None:
        # HWPX splits a line into runs at every formatting change; they must not become separate lines.
        path = make_hwpx(
            tmp_path, f'<hp:sec xmlns:hp="{HP}">{paragraph("계약", " 기간은 ", "24개월")}</hp:sec>'
        )
        assert "계약 기간은 24개월" in HwpxParser().parse(path, max_chars=10_000).text

    def test_keeps_table_cells_apart(self, tmp_path: Path) -> None:
        # A flat text-run scan would collapse rows into one line, misattributing values across cells.
        table = (
            f'<hp:sec xmlns:hp="{HP}"><hp:p><hp:tbl>'
            f"<hp:tr><hp:tc>{paragraph('사업')}</hp:tc><hp:tc>{paragraph('금액')}</hp:tc></hp:tr>"
            f"<hp:tr><hp:tc>{paragraph('아폴로')}</hp:tc><hp:tc>{paragraph('120,000')}</hp:tc></hp:tr>"
            f"<hp:tr><hp:tc>{paragraph('제피르')}</hp:tc><hp:tc>{paragraph('98,000')}</hp:tc></hp:tr>"
            f"</hp:tbl></hp:p></hp:sec>"
        )
        text = HwpxParser().parse(make_hwpx(tmp_path, table), max_chars=10_000).text

        apollo_line = next(line for line in text.splitlines() if "아폴로" in line)
        assert "120,000" in apollo_line
        assert "98,000" not in apollo_line

    def test_sections_are_read_in_numeric_order(self, tmp_path: Path) -> None:
        # Lexicographic sort would put section10 before section9.
        path = tmp_path / "many.hwpx"
        with zipfile.ZipFile(path, "w") as archive:
            for index in (0, 9, 10):
                archive.writestr(
                    f"Contents/section{index}.xml",
                    f'<hp:sec xmlns:hp="{HP}">{paragraph(f"SECTION-{index}")}</hp:sec>',
                )
        text = HwpxParser().parse(path, max_chars=10_000).text
        assert text.index("SECTION-0") < text.index("SECTION-9") < text.index("SECTION-10")

    def test_survives_one_unreadable_section(self, tmp_path: Path) -> None:
        path = tmp_path / "partly-broken.hwpx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "Contents/section0.xml",
                f'<hp:sec xmlns:hp="{HP}">{paragraph("좋은 내용")}</hp:sec>',
            )
            archive.writestr("Contents/section1.xml", "<hp:sec><<<not xml")
        extraction = HwpxParser().parse(path, max_chars=10_000)
        assert "좋은 내용" in extraction.text
        assert extraction.truncated

    def test_legacy_hwp_says_what_to_do_about_it(self, tmp_path: Path) -> None:
        path = tmp_path / "old.hwp"
        path.write_bytes(b"\xd0\xcf\x11\xe0not a zip")
        with pytest.raises(ParserUnavailableError, match=r"re-save it as .hwpx"):
            HwpxParser().parse(path, max_chars=10_000)


class TestPlainText:
    def test_reads_legacy_korean_encodings(self, tmp_path: Path) -> None:
        # cp949 is a common legacy encoding for Korean text exports.
        path = tmp_path / "legacy.txt"
        path.write_bytes("아폴로 사업 보고서".encode("cp949"))
        assert "아폴로 사업 보고서" in PlainTextParser().parse(path, max_chars=1000).text

    def test_splits_markdown_on_headings(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.md"
        path.write_text("preamble\n\n# One\nalpha\n\n# Two\nbeta\n", encoding="utf-8")
        extraction = PlainTextParser().parse(path, max_chars=1000)
        assert [s.heading for s in extraction.sections] == [None, "One", "Two"]
        assert "alpha" in extraction.sections[1].text

    def test_truncation_is_recorded_not_silent(self, tmp_path: Path) -> None:
        path = tmp_path / "long.txt"
        path.write_text("x" * 5000, encoding="utf-8")
        extraction = PlainTextParser().parse(path, max_chars=100)
        assert extraction.truncated is True
        assert len(extraction.text) <= 100


class TestCsv:
    def test_renders_a_labelled_table(self, tmp_path: Path) -> None:
        path = tmp_path / "data.csv"
        path.write_text("project,amount\nApollo,120000\n", encoding="utf-8")
        text = CsvParser().parse(path, max_chars=1000).text
        assert "| project | amount |" in text
        assert "| Apollo | 120000 |" in text


def test_spreadsheet_rows_are_not_silently_capped() -> None:
    class Sheet:
        title = "Data"

        def iter_rows(self, *, values_only: bool):  # type: ignore[no-untyped-def]
            assert values_only
            return ((f"row-{index}",) for index in range(250))

    class Workbook:
        def __init__(self) -> None:
            self.worksheets = [Sheet()]

    section = next(_sheets(Workbook()))
    assert "row-249" in section.text


class TestRegistry:
    def test_dispatches_by_extension(self, tmp_path: Path) -> None:
        registry = build_registry()
        assert registry.for_path(tmp_path / "a.hwpx").name == "hwpx"
        assert registry.for_path(tmp_path / "a.pdf").name == "pypdf"
        assert registry.for_path(tmp_path / "a.md").name == "plain"

    def test_unknown_extension_names_the_fix(self, tmp_path: Path) -> None:
        with pytest.raises(ParserUnavailableError, match="bismuth-kb\\[parsers\\]"):
            build_registry().for_path(tmp_path / "mystery.xyz")
