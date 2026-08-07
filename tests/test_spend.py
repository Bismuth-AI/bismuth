"""Tokens and price: summed, attributed to one document, and drained so nothing piles up."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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


class TestSpendOverHttp:
    def test_an_upload_reports_what_it_cost(self, client: TestClient) -> None:
        body = client.post(
            "/api/documents",
            files={"files": ("계약.txt", "아폴로 계약서".encode(), "text/plain")},
        ).json()[0]

        # FakeLLM records a Usage per call with no price, so calls are real and cost is not.
        assert body["spend"]["calls"] > 0
        assert body["spend"]["cost_usd"] is None

    def test_each_document_is_billed_for_its_own_calls(self, client: TestClient) -> None:
        """Draining around one document is what attributes the bill. Without it the counts
        would climb — 4, 7, 10 — as every document inherited its predecessors'."""
        counts = [
            client.post(
                "/api/documents", files={"files": (name, f"문서 {name}".encode(), "text/plain")}
            ).json()[0]["spend"]["calls"]
            for name in ("a.txt", "b.txt", "c.txt")
        ]

        # Per-document cost is not uniform and is not meant to be: the first builds the
        # tree, and subdivision is asked on a doubling schedule rather than every time.
        # What must hold is that nobody pays for the documents before them.
        assert counts[2] < sum(counts[:2])
        assert counts[2] <= max(counts[:2])

    def test_a_duplicate_costs_almost_nothing(self, client: TestClient) -> None:
        payload = {"files": ("계약.txt", "같은 바이트".encode(), "text/plain")}
        client.post("/api/documents", files=payload)
        again = client.post(
            "/api/documents",
            files={"files": ("다른이름.txt", "같은 바이트".encode(), "text/plain")},
        ).json()[0]

        assert again["duplicate"] is True
        assert again["spend"]["calls"] == 0  # the hash check happens before any model call

    def test_draining_is_what_stops_usage_piling_up(self, client: TestClient) -> None:
        """Nothing read the adapters' usage lists before this, so nothing ever emptied them."""
        engine = client.app.state.engine  # type: ignore[attr-defined]
        for name in ("a.txt", "b.txt", "c.txt"):
            client.post(
                "/api/documents", files={"files": (name, f"문서 {name}".encode(), "text/plain")}
            )
        assert engine.llm.drain_usage() == []
