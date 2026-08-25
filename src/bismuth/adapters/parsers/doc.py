"""Legacy Word binary document extraction through unword."""

from __future__ import annotations

from pathlib import Path

from bismuth.adapters.parsers.registry import build_extraction, require
from bismuth.domain.document import Extraction, Section
from bismuth.domain.errors import ParserUnavailableError


class DocParser:
    """Read legacy Word 97-2003 ``.doc`` files without converting them."""

    @property
    def name(self) -> str:
        return "unword"

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".doc"})

    def warm(self) -> None:
        require("unword", "Reading .doc needs unword: pip install 'bismuth-kb[parsers]'")

    def parse(self, path: Path, *, max_chars: int) -> Extraction:
        self.warm()
        from unword import parse_doc

        try:
            parsed = parse_doc(path.read_bytes())
            text = parsed.body_text.strip()
        except Exception as exc:
            raise ParserUnavailableError(f"{path.name} is not a readable .doc: {exc}") from exc

        if not text:
            raise ParserUnavailableError(f"{path.name} has no extractable text")
        return build_extraction(
            [Section(heading=None, text=text, order=0)], parser=self.name, max_chars=max_chars
        )
