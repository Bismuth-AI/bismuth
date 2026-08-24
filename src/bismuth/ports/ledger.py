"""Persistent model-spend boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bismuth.ports.llm import Spend


@runtime_checkable
class SpendLedger(Protocol):
    """Append-only record of what has been spent against one vault."""

    def record(self, spend: Spend) -> None:
        """Add one piece of work. Nothing is ever rewritten or removed."""
        ...

    def total(self) -> Spend:
        """Everything spent against this vault, summed."""
        ...
