"""The spend boundary: what this vault has cost, kept because a session cannot keep it.

The running total used to live in the browser tab, on the argument that it answered
"what did that just cost me?" and that a number surviving a restart would imply an
accounting this program does not do. It does not survive a refresh, which is the moment
people actually ask the question.
"""

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
