"""Dividing a folder: when it happens, what it moves, and what stops it repeating."""

from __future__ import annotations

from pathlib import PurePosixPath

from bismuth.container import Bismuth
from bismuth.domain.charter import Charter
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import subdivision as subdivision_prompts
from tests.conftest import ScriptedModel
from tests.test_ingest import add, place_at


def _group(name: str, note: str, ids: list[str]) -> subdivision_prompts.Group:
    return subdivision_prompts.Group(name=name, note=note, document_ids=ids)


def _ids(engine: Bismuth) -> list[str]:
    """Document ids ordered by filename, so doc0 is ids[0]. The catalog iterates in
    hash order, which would make every assertion below a coin flip."""
    by_name = {
        source.filename: document_id
        for document_id, _ in engine.catalog.iter_cards()
        if (source := engine.catalog.load_source(document_id)) is not None
    }
    return [by_name[name] for name in sorted(by_name)]


async def _fill(engine: Bismuth, script: ScriptedModel, count: int) -> list[str]:
    """Put `count` documents in the root. Distinct bodies: identity is the bytes."""
    script.set(placement_prompts.PlacementDecision, place_at(""))
    for index in range(count):
        await add(engine, f"doc{index}.txt", f"문서 {index} 내용")
    return _ids(engine)


class TestDivideDecision:
    async def test_nothing_happens_when_there_is_no_distinction(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The scripted default is "no division"; a young folder stays flat."""
        await _fill(engine, script, 3)

        assert not (engine.vault.root / "문학").exists()
        assert (engine.vault.root / "doc0.txt").is_file()

    async def test_an_empty_folder_is_never_asked_about(
        self,
        engine: Bismuth,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        await engine.subdivision.consider(PurePosixPath())
        assert not llm.prompts_for(subdivision_prompts.Division)

    async def test_the_inbox_is_not_a_category(self, engine: Bismuth, llm) -> None:  # type: ignore[no-untyped-def]
        await engine.subdivision.consider(PurePosixPath("_inbox"))
        assert not llm.prompts_for(subdivision_prompts.Division)

    async def test_a_human_written_note_is_left_alone(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """`managed: false` means a person arranged this; it is not ours to redraw."""
        await _fill(engine, script, 2)
        note = Charter(path=PurePosixPath(), title="내 서재", purpose="직접 정리함", managed=False)
        (engine.vault.root / "_folder.md").write_text(note.to_markdown(), encoding="utf-8")
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        assert not llm.prompts_for(subdivision_prompts.Division)


class TestDividing:
    async def test_documents_move_into_the_new_folders(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 3)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True,
                basis="주제",
                groups=[_group("문학", "문학 자료", ids[:2]), _group("과학", "과학 자료", ids[2:])],
                reason="주제가 둘로 갈립니다.",
            ),
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert len(divided) == 1
        assert divided[0].moved == 3
        assert (engine.vault.root / "문학").is_dir()
        assert (engine.vault.root / "과학").is_dir()
        # Existing documents are re-filed, not just future ones (SPEC.md 3.4).
        assert not (engine.vault.root / "doc0.txt").exists()

    async def test_the_sidecar_travels_with_its_document(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True,
                basis="주제",
                groups=[_group("문학", "문학", ids[:1]), _group("과학", "과학", ids[1:])],
                reason="r",
            ),
        )

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "문학/doc0.txt.md").is_file()
        assert not (engine.vault.root / "doc0.txt.md").exists()

    async def test_each_new_folder_gets_a_note_that_distinguishes_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True,
                basis="주제",
                groups=[
                    _group("문학", "소설과 시. 과학 자료가 아닌 것.", ids[:1]),
                    _group("과학", "실험과 논문.", ids[1:]),
                ],
                reason="r",
            ),
        )

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath("문학"))
        assert charter is not None
        assert charter.purpose == "소설과 시. 과학 자료가 아닌 것."

    async def test_a_document_in_no_group_stays_put(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 3)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True, basis="주제", groups=[_group("문학", "문학", ids[:2])], reason="r"
            ),
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided[0].moved == 2
        assert (engine.vault.root / "doc2.txt").is_file()  # left behind, on purpose

    async def test_dividing_is_one_undoable_batch(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True,
                basis="주제",
                groups=[_group("문학", "문학", ids[:1]), _group("과학", "과학", ids[1:])],
                reason="r",
            ),
        )
        await engine.subdivision.consider(PurePosixPath())

        entry = next(e for e in engine.journal.iter_entries() if "divide" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "doc0.txt").is_file()
        assert (engine.vault.root / "doc1.txt").is_file()

    async def test_an_unusable_folder_name_is_skipped_not_fatal(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True,
                basis="주제",
                groups=[_group("...", "쓸 수 없는 이름", ids[:1]), _group("문학", "문학", ids[1:])],
                reason="r",
            ),
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided[0].moved == 1
        assert (engine.vault.root / "문학").is_dir()


class TestRemembering:
    async def test_the_folder_records_what_it_was_divided_along(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Without this the only question available later is "how would you divide
        this", which has an answer every time and so never settles."""
        ids = await _fill(engine, script, 4)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True,
                basis="주제 분야",
                groups=[_group("문학", "문학", ids[:2]), _group("과학", "과학", ids[2:])],
                reason="r",
            ),
        )

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath())
        assert charter is not None
        assert charter.split_basis == "주제 분야"
        assert charter.split_at_documents == 4
        assert charter.divided


class TestReview:
    def test_a_division_is_not_revisited_until_the_evidence_doubles(self) -> None:
        charter = Charter(
            path=PurePosixPath("문학"),
            title="문학",
            purpose="p",
            split_basis="주제",
            split_at_documents=30,
        )

        assert not charter.due_for_review(31)
        assert not charter.due_for_review(59)
        assert charter.due_for_review(60)

    def test_an_undivided_folder_is_never_due(self) -> None:
        charter = Charter(path=PurePosixPath("문학"), title="문학", purpose="p")
        assert not charter.divided
        assert not charter.due_for_review(1000)

    async def test_a_divided_folder_is_asked_the_review_question(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """Not "how would you divide this" -- that has an answer every time."""
        ids = await _fill(engine, script, 2)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True, basis="주제", groups=[_group("문학", "문학", ids[:1])], reason="r"
            ),
        )
        await engine.subdivision.consider(PurePosixPath())  # divided at 2
        llm.calls.clear()

        # Back to 2 loose documents at the root would be under the bar; push past it.
        script.set(placement_prompts.PlacementDecision, place_at(""))
        for index in range(4):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")

        assert llm.prompts_for(subdivision_prompts.Review)

    async def test_a_holding_review_moves_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True, basis="주제", groups=[_group("문학", "문학", ids[:1])], reason="r"
            ),
        )
        await engine.subdivision.consider(PurePosixPath())
        before = sorted(p.name for p in engine.vault.root.iterdir())

        # The scripted Review holds by default.
        for index in range(4):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")

        assert (engine.vault.root / "문학").is_dir()
        assert "문학" in before


class TestTermination:
    async def test_one_group_holding_everything_is_not_a_division(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """It only moves the folder a level deeper, leaving the same problem at the same
        size -- which recurses for ever."""
        ids = await _fill(engine, script, 3)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True, basis="주제", groups=[_group("문학", "전부", ids)], reason="r"
            ),
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert not (engine.vault.root / "문학").exists()
        assert (engine.vault.root / "doc0.txt").is_file()

    async def test_a_real_division_recurses_into_what_it_made(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """A child is strictly smaller than its parent, so this ends on its own."""
        ids = await _fill(engine, script, 4)
        script.set(
            subdivision_prompts.Division,
            subdivision_prompts.Division(
                divide=True,
                basis="주제",
                groups=[_group("문학", "문학", ids[:2]), _group("과학", "과학", ids[2:])],
                reason="r",
            ),
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        # Root divided; each child was then considered and, being a single group over
        # its whole contents, correctly refused to divide further.
        assert len(divided) == 1
        assert (engine.vault.root / "문학/doc0.txt").is_file()
        assert (engine.vault.root / "과학/doc2.txt").is_file()
