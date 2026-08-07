"""Drawing a class out of a folder: when it happens, what it moves, what it leaves."""

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


def _emerges(script: ScriptedModel, name: str, note: str, ids: list[str]) -> None:
    """Script a class coming out of the pile: its name, then who belongs to it."""
    script.set(
        subdivision_prompts.Emerging,
        subdivision_prompts.Emerging(
            emerged=True, name=name, note=note, reason="이 부류가 두껍습니다."
        ),
    )
    script.set(
        subdivision_prompts.Members,
        subdivision_prompts.Members(document_ids=ids, reason="이 사인 아래 놓입니다."),
    )


def _by_name(engine: Bismuth) -> dict[str, str]:
    return {
        source.filename: document_id
        for document_id, _ in engine.catalog.iter_cards()
        if (source := engine.catalog.load_source(document_id)) is not None
    }


def _ids(engine: Bismuth) -> list[str]:
    """Document ids ordered by filename, so doc0 is ids[0]. The catalog iterates in
    hash order, which would make every assertion below a coin flip."""
    by_name = _by_name(engine)
    return [by_name[name] for name in sorted(by_name)]


async def _fill(engine: Bismuth, script: ScriptedModel, count: int) -> list[str]:
    """Put `count` documents in the root. Distinct bodies: identity is the bytes."""
    script.set(placement_prompts.PlacementDecision, place_at(""))
    for index in range(count):
        await add(engine, f"doc{index}.txt", f"문서 {index} 내용")
    return _ids(engine)


class TestDivideDecision:
    async def test_nothing_happens_when_nothing_has_gathered(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The scripted default is "nothing emerged"; a young folder stays flat."""
        await _fill(engine, script, 4)

        assert not (engine.vault.root / "문학").exists()
        assert (engine.vault.root / "doc0.txt").is_file()

    async def test_an_empty_folder_is_never_asked_about(
        self,
        engine: Bismuth,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        await engine.subdivision.consider(PurePosixPath())
        assert not llm.prompts_for(subdivision_prompts.Emerging)

    async def test_the_inbox_is_not_a_category(self, engine: Bismuth, llm) -> None:  # type: ignore[no-untyped-def]
        await engine.subdivision.consider(PurePosixPath("_inbox"))
        assert not llm.prompts_for(subdivision_prompts.Emerging)

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

        assert not llm.prompts_for(subdivision_prompts.Emerging)

    async def test_naming_a_class_but_claiming_nobody_creates_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", [])

        assert await engine.subdivision.consider(PurePosixPath()) == []
        assert not (engine.vault.root / "문학").exists()


class TestDrawingOutAClass:
    async def test_the_documents_of_that_class_move_into_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert len(divided) == 1
        assert divided[0].moved == 2
        assert (engine.vault.root / "문학").is_dir()
        # Existing documents are re-filed, not just future ones (SPEC.md 3.4).
        assert not (engine.vault.root / "doc0.txt").exists()

    async def test_the_rest_stay_and_are_given_no_name(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The whole point. A partition has to account for every document, so the
        leftovers get a folder called "everything else"; drawing one class out cannot
        express that, and SPEC.md 3.4 says they stay in the parent."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        folders = [
            p.name
            for p in engine.vault.root.iterdir()
            if p.is_dir() and p.name not in ("_inbox", ".bismuth")
        ]
        assert folders == ["문학"]  # no sibling was invented to hold doc2 and doc3
        assert (engine.vault.root / "doc2.txt").is_file()
        assert (engine.vault.root / "doc3.txt").is_file()

    async def test_one_look_can_only_produce_one_folder(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The structural half of the fix: a reply carries one name, so a single look
        cannot lay down a class and a bucket for what it did not cover."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided[0].created == (PurePosixPath("문학"),)

    async def test_the_sidecar_travels_with_its_document(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        _emerges(script, "문학", "문학", ids[:1])

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "문학/doc0.txt.md").is_file()
        assert not (engine.vault.root / "doc0.txt.md").exists()

    async def test_the_new_folder_gets_a_note_that_distinguishes_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        _emerges(script, "문학", "소설과 시. 과학 자료가 아닌 것.", ids[:1])

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath("문학"))
        assert charter is not None
        assert charter.purpose == "소설과 시. 과학 자료가 아닌 것."

    async def test_drawing_a_class_out_is_one_undoable_batch(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        _emerges(script, "문학", "문학", ids[:1])
        await engine.subdivision.consider(PurePosixPath())

        entry = next(e for e in engine.journal.iter_entries() if "divide" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "doc0.txt").is_file()
        assert (engine.vault.root / "doc1.txt").is_file()

    async def test_an_unusable_folder_name_is_skipped_not_fatal(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        _emerges(script, "...", "쓸 수 없는 이름", ids[:1])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert (engine.vault.root / "doc0.txt").is_file()


class TestRemembering:
    async def test_the_folder_records_what_it_was_divided_along(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Without this the only question available later is "how would you divide
        this", which has an answer every time and so never settles."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath())
        assert charter is not None
        assert "문학" in charter.split_basis
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

    async def test_a_divided_folder_is_measured_through_its_subtree(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Dividing moves documents into a child. Counting only what sits directly in the
        parent would undercount on the way out, and the division would be looked at again
        far too early."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath())
        assert charter is not None
        # Four, not the two still sitting loose: the pair that moved is a level down but
        # still under the root, and the doubling rule is measured against all of it.
        assert charter.split_at_documents == 4

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
        _emerges(script, "문학", "문학", ids[:1])
        await engine.subdivision.consider(PurePosixPath())  # divided at 2
        llm.calls.clear()

        script.set(placement_prompts.PlacementDecision, place_at(""))
        for index in range(4):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")

        assert llm.prompts_for(subdivision_prompts.Review)

    async def test_a_holding_review_re_arms_the_doubling(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """Upholding a division is a judgement made at this size and is recorded as one.
        Unrecorded, the folder stays past its doubling for ever and is asked on every
        single ingest from then on -- and, worse, that answer used to end the look, so
        the folder was never asked what else had grown in it."""
        ids = await _fill(engine, script, 2)
        _emerges(script, "문학", "문학", ids[:1])
        await engine.subdivision.consider(PurePosixPath())  # divided, recorded at 2

        script.set(placement_prompts.PlacementDecision, place_at(""))
        for index in range(4):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")

        charter = engine.charters.load(PurePosixPath())
        assert charter is not None
        assert charter.split_at_documents > 2  # moved up with the holding review
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())
        assert not llm.prompts_for(subdivision_prompts.Review)  # not due again yet

    async def test_a_holding_review_still_lets_a_new_class_come_out(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """The two questions are different jobs. "The old split still holds" must not
        answer "has anything new grown here", or the folder freezes at the size it was
        first divided at -- measured: nineteen of twenty-nine documents stranded."""
        ids = await _fill(engine, script, 2)
        _emerges(script, "문학", "문학", ids[:1])
        await engine.subdivision.consider(PurePosixPath())

        script.set(placement_prompts.PlacementDecision, place_at(""))
        for index in range(3):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")

        # Force the root well past its doubling so the review certainly runs first.
        root = engine.charters.load(PurePosixPath())
        assert root is not None
        (engine.vault.root / "_folder.md").write_text(
            root.model_copy(update={"split_at_documents": 1}).to_markdown(), encoding="utf-8"
        )

        # doc0 went into 문학; these two are still loose at the root.
        by_name = _by_name(engine)
        _emerges(script, "과학", "과학 자료", [by_name["more0.txt"], by_name["more1.txt"]])
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        assert llm.prompts_for(subdivision_prompts.Review)  # the review did run
        assert (engine.vault.root / "과학").is_dir()  # and did not swallow the other question

    async def test_a_holding_review_moves_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 2)
        _emerges(script, "문학", "문학", ids[:1])
        await engine.subdivision.consider(PurePosixPath())
        before = sorted(p.name for p in engine.vault.root.iterdir())

        # The scripted Review holds by default.
        for index in range(4):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")

        assert (engine.vault.root / "문학").is_dir()
        assert "문학" in before


class TestTermination:
    async def test_a_class_that_takes_everything_is_not_a_division(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """It only moves the folder a level deeper, leaving the same problem at the same
        size -- which recurses for ever."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "전부", ids)

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert not (engine.vault.root / "문학").exists()
        assert (engine.vault.root / "doc0.txt").is_file()

    async def test_what_was_just_created_is_not_immediately_re_judged(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The new folder was formed a moment ago from a judgement over these same
        documents; asking it again adds no evidence, only depth. With the recursion in,
        one ingest built 철학/현상학/체화된 인지, a document per level. It is asked as soon
        as anything lands in it, which is what actually counts as new evidence."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert len(divided) == 1
        assert (engine.vault.root / "문학/doc0.txt").is_file()
        assert (engine.vault.root / "문학/doc1.txt").is_file()
        assert not (engine.vault.root / "문학/문학").exists()

    async def test_a_class_may_not_carry_its_parents_name(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Asked what has grown in 철학 the model answers 철학 -- true, and useless. It
        is caught below only when the class takes everything; taking three of five is
        how 철학/철학 and 과학·기술 연구/과학·기술 연구 were built."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())

        # Now 문학 holds two, and the model proposes 문학 again for one of them.
        _emerges(script, "문학", "또 문학", ids[:1])
        divided = await engine.subdivision.consider(PurePosixPath("문학"))

        assert divided == []
        assert not (engine.vault.root / "문학/문학").exists()
