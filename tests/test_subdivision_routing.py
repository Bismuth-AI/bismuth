"""Putting a loose document behind a sign that already stands."""

from __future__ import annotations

from pathlib import PurePosixPath

from bismuth.container import Bismuth
from bismuth.prompts import subdivision as subdivision_prompts
from tests.conftest import ScriptedModel
from tests.subdivision_helpers import _emerges, _fill, _routing


class TestRoutingSeesTheNotes:
    async def test_each_existing_sign_carries_its_note(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        """Shown names only, routing put 가상자산 이용자 보호법 시행령 into 데이터 산업
        관련 법령 -- crypto reads as digital from a two-word name, and the note that
        ruled it out was not in the prompt."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "소설과 시. 과학 자료가 아닌 것.", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        routing = [p for p in llm.prompts_for(None) if "\n  [F" in p.user and "SIGNS:" in p.user]
        assert routing
        assert all("소설과 시" in p.user for p in routing)


class TestRoutingRemembersWhatItAsked:
    """8,416 routing questions placed twelve documents in one run -- 0.14% -- because
    every arrival asked the whole loose pile again, and the pile only grew."""

    async def test_a_document_that_said_stay_is_not_asked_again(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 8)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))
        await engine.subdivision.consider(PurePosixPath())
        first = [p for p in llm.prompts_for(None) if _routing(p)]
        assert first, "routing did run once"
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        again = [p for p in llm.prompts_for(None) if _routing(p)]
        assert not again

    async def test_a_new_sign_makes_it_ask_again(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """The answer depended on the signs it was shown, so the memory lasts as long as
        they do and not one arrival longer."""
        ids = await _fill(engine, script, 8)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()
        # A second shelf goes up, so every document is looking at a different list.
        _emerges(script, "과학", "과학 자료", ids[2:4])
        await engine.subdivision.consider(PurePosixPath())
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        again = [p for p in llm.prompts_for(None) if _routing(p)]
        assert again
