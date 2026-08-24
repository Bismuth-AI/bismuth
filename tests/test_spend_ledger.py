"""What a vault has cost has to outlive the tab that spent it."""

from __future__ import annotations

import time
from pathlib import Path

from bismuth.adapters.ledger import JsonlSpendLedger
from bismuth.ports.llm import Spend, Usage


def _spend(cost: float, *, calls: int = 1) -> Spend:
    return Spend.of([Usage(model="m", input_tokens=100, output_tokens=20, cost_usd=cost)] * calls)


class TestLedger:
    def test_an_empty_vault_has_spent_nothing(self, tmp_path: Path) -> None:
        assert JsonlSpendLedger(tmp_path / "spend.jsonl").total().calls == 0

    def test_the_total_survives_a_restart(self, tmp_path: Path) -> None:
        """The whole point: a new process, and a new browser tab, still know."""
        path = tmp_path / "spend.jsonl"
        JsonlSpendLedger(path).record(_spend(0.01))
        JsonlSpendLedger(path).record(_spend(0.02))

        reopened = JsonlSpendLedger(path).total()

        assert reopened.calls == 2
        assert reopened.cost_usd == 0.03

    def test_the_writer_agrees_with_a_fresh_reader(self, tmp_path: Path) -> None:
        """Summing the file after appending to it counts the new line twice, and only
        for the instance that wrote it -- so the running header drifts from the truth."""
        path = tmp_path / "spend.jsonl"
        ledger = JsonlSpendLedger(path)

        ledger.record(_spend(0.01))
        ledger.record(_spend(0.02))

        assert ledger.total() == JsonlSpendLedger(path).total()

    def test_work_that_cost_nothing_is_not_recorded(self, tmp_path: Path) -> None:
        """A duplicate upload makes no model call; a line for it would be noise."""
        path = tmp_path / "spend.jsonl"
        ledger = JsonlSpendLedger(path)

        ledger.record(Spend())

        assert not path.exists()
        assert ledger.total().calls == 0

    def test_a_damaged_line_costs_the_line_not_the_program(self, tmp_path: Path) -> None:
        path = tmp_path / "spend.jsonl"
        JsonlSpendLedger(path).record(_spend(0.01))
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{ this is not json\n")

        assert JsonlSpendLedger(path).total().calls == 1

    def test_an_unpriced_call_is_counted_but_not_priced(self, tmp_path: Path) -> None:
        """Local and unlisted models have no published rate; the total is a floor."""
        path = tmp_path / "spend.jsonl"
        ledger = JsonlSpendLedger(path)

        ledger.record(Spend.of([Usage(model="local", input_tokens=5, output_tokens=1)]))

        total = ledger.total()
        assert total.calls == 1
        assert total.priced_calls == 0
        assert not total.fully_priced


class TestReportedToTheUI:
    def test_status_carries_the_vault_total(self, client) -> None:  # type: ignore[no-untyped-def]
        """Status reports the persisted vault total."""
        before = client.get("/api/status").json()["spend"]
        assert before["calls"] == 0

        submitted = client.post(
            "/api/batches",
            files=[
                ("files", ("contract.txt", b"\xec\x95\x84\xed\x8f\xb4\xeb\xa1\x9c", "text/plain"))
            ],
        ).json()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            batch = client.get(f"/api/batches/{submitted['id']}").json()
            if batch["status"] == "done":
                break
            time.sleep(0.01)

        after = client.get("/api/status").json()["spend"]
        assert after["calls"] > 0
