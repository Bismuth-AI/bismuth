"""Tokens and price: summed, attributed to one document, and drained so nothing piles up."""

from __future__ import annotations

import pytest

from bismuth.ports.llm import Spend, Usage


def _usage(*, cost: float | None = 0.001, inp: int = 100, out: int = 20) -> Usage:
    return Usage(model="m", input_tokens=inp, output_tokens=out, cost_usd=cost)


class TestSpend:
    def test_nothing_spent_is_all_zeros(self) -> None:
        empty = Spend.of([])
        assert (empty.calls, empty.tokens, empty.cost_usd) == (0, 0, None)
        assert not empty.fully_priced

    def test_calls_are_summed(self) -> None:
        spend = Spend.of([_usage(inp=100, out=20), _usage(inp=50, out=10)])
        assert spend.calls == 2
        assert (spend.input_tokens, spend.output_tokens, spend.tokens) == (150, 30, 180)
        assert spend.cost_usd == pytest.approx(0.002)
        assert spend.fully_priced

    def test_an_unpriced_model_is_reported_not_guessed(self) -> None:
        """Local and unlisted models have no published rate; inventing one would be worse."""
        spend = Spend.of([_usage(cost=None), _usage(cost=None)])
        assert spend.calls == 2
        assert spend.cost_usd is None
        assert spend.priced_calls == 0

    def test_a_partly_priced_total_says_so(self) -> None:
        spend = Spend.of([_usage(cost=0.005), _usage(cost=None)])
        assert spend.cost_usd == pytest.approx(0.005)
        assert spend.priced_calls == 1
        assert not spend.fully_priced  # the figure is a floor

    def test_retries_are_carried_through(self) -> None:
        spend = Spend.of([Usage(model="m", retries=2), Usage(model="m", retries=1)])
        assert spend.retries == 3

    def test_adding_two_spends_keeps_the_pricing_caveat(self) -> None:
        combined = Spend.of([_usage(cost=0.01)]) + Spend.of([_usage(cost=None)])
        assert combined.calls == 2
        assert combined.priced_calls == 1
        assert combined.cost_usd == pytest.approx(0.01)

    def test_adding_to_nothing_changes_nothing(self) -> None:
        spend = Spend.of([_usage(cost=0.01)])
        assert (spend + Spend()).model_dump() == spend.model_dump()
