"""JSONL spend ledger: one line per piece of work, summed on first read and kept in memory."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from bismuth.ports.llm import Spend

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "spend.jsonl"


class JsonlSpendLedger:
    """Append spend records and cache their running total."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._total: Spend | None = None

    def record(self, spend: Spend) -> None:
        if not spend.calls:
            return
        running = self.total()
        line = json.dumps(spend.model_dump(mode="json"), ensure_ascii=False)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self._total = running + spend

    def total(self) -> Spend:
        if self._total is None:
            self._total = self._read()
        return self._total

    def _read(self) -> Spend:
        """Sum valid records and report damaged lines."""
        if not self._path.exists():
            return Spend()
        total = Spend()
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                total = total + Spend.model_validate(json.loads(line))
            except (ValueError, TypeError) as exc:
                logger.warning("%s line %d is unreadable, skipping: %s", self._path, number, exc)
        return total
