"""Drawing a class out of a folder: when it happens, what it moves, what it leaves."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from unittest import mock

import pytest
from pydantic import ValidationError

from bismuth.container import Bismuth
from bismuth.domain.charter import CHARTER_SCHEMA_VERSION, Charter
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import subdivision as subdivision_prompts
from bismuth.services import subdivision as subdivision_service
from tests.conftest import ScriptedModel
from tests.test_ingest import add, place_at


def _group(name: str, note: str, ids: list[str]) -> subdivision_prompts.Group:
    return subdivision_prompts.Group(name=name, note=note, document_ids=ids)


def _review_prompts(llm) -> list:  # type: ignore[no-untyped-def]
    """Review is one closed HOLDS/FAILS question per check, not a structured call."""
    return [p for p in llm.prompts_for(None) if "HOLDS or FAILS" in p.system]


def _emerges(
    script: ScriptedModel, name: str, note: str, ids: list[str], *, axis: str = "주제"
) -> None:
    """Script a class coming out of the pile: its name, then who belongs to it."""
    script.set(
        subdivision_prompts.Emerging,
        subdivision_prompts.Emerging(
            emerged=True,
            axis=axis,
            axis_question=f"어느 {axis}에 속하는가?",
            name=name,
            sign=note,
        ),
    )
    script.set(
        subdivision_prompts.Members,
        subdivision_prompts.Members(document_ids=ids),
    )
    # Membership is one closed SHELF/STAY choice per document since ADR-0014; the
    # Members schema above no longer reaches this path.
    script.set_members(ids)


def _replacement(
    script: ScriptedModel,
    *,
    basis: str,
    question: str,
    groups: list[subdivision_prompts.Group],
) -> None:
    """Script the production sketch-then-bounded-assignment replacement contract."""
    script.set(
        subdivision_prompts.ReplacementSketch,
        subdivision_prompts.ReplacementSketch(
            basis=basis,
            basis_question=question,
            signs=[
                subdivision_prompts.ReplacementSign(name=group.name, sign=group.note)
                for group in groups
            ],
        ),
    )
    # Assignment is one closed G### choice per document (ADR-0014).
    script.set_assignments(
        {
            document_id: f"G{index:03d}"
            for index, group in enumerate(groups, start=1)
            for document_id in group.document_ids
        }
    )

    def assign(prompt, schema):  # type: ignore[no-untyped-def]
        shown = set(re.findall(r"\[(D\d{4})\]", prompt.user))
        return subdivision_prompts.ReplacementAssignments(
            groups=[
                subdivision_prompts.ReplacementAssignment(
                    folder_id=f"G{index:03d}",
                    document_ids=[item for item in group.document_ids if item in shown],
                )
                for index, group in enumerate(groups, start=1)
                if shown.intersection(group.document_ids)
            ]
        )

    script.set(subdivision_prompts.ReplacementAssignments, assign)


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
    """Put documents in root and return the short handles shown in one maintenance view."""
    script.set(placement_prompts.PlacementDecision, place_at(""))
    for index in range(count):
        await add(engine, f"doc{index}.txt", f"문서 {index} 내용")
    return [f"D{index:04d}" for index in range(1, count + 1)]


class TestDivideDecision:
    async def test_model_prompts_use_request_local_handles_not_catalog_hashes(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        await _fill(engine, script, 4)

        prompts = llm.prompts_for(subdivision_prompts.Emerging)
        assert prompts
        assert all(not re.search(r"\[[0-9a-f]{16}(?:~\d+)?\]", item.user) for item in prompts)
        assert "[D0001]" in prompts[-1].user

    async def test_membership_is_one_bounded_question_per_document(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        """Output size is constant in the archive: one closed choice, one document.

        This replaced a packeted call returning a list of ids, where a long enough list
        could omit or duplicate one (ADR-0014).
        """
        documents = [(f"D{index:04d}", f"문서 {index}") for index in range(1, 31)]
        script.set_members([f"D{index:04d}" for index in range(1, 11)])

        result = await engine.maintenance._find_members(  # type: ignore[attr-defined]
            folder=PurePosixPath(),
            purpose="자료",
            documents=documents,
            name="자료",
        )

        calls = llm.prompts_for(None)
        assert len(calls) == len(documents)
        # One document per question: a reply can name no other document, in any archive.
        assert all(len(re.findall(r"\[(D\d{4})\]", call.user)) == 1 for call in calls)
        assert result.document_ids == [f"D{index:04d}" for index in range(1, 11)]

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

    async def test_a_proposal_that_translates_the_corpus_is_rejected(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(
            script,
            "Safety and Occupational Health",
            "Regulations concerning workplace safety and occupational health.",
            ids[:2],
            axis="subject domain",
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert not (engine.vault.root / "Safety and Occupational Health").exists()

    async def test_a_later_class_is_audited_with_all_existing_siblings(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())

        llm.calls.clear()
        _emerges(script, "과학", "과학 자료", ids[2:4])
        await engine.subdivision.consider(PurePosixPath())

        audit_prompt = llm.prompts_for(subdivision_prompts.BoundaryAudit)[-1].user
        assert "문학" in audit_prompt
        assert "과학" in audit_prompt
        assert "current=문학/" in audit_prompt

    async def test_an_essay_length_folder_note_falls_back_to_derived_state(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """An unusable sign degrades to the derived one; it never fails an ingest."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료에 관한 상세 분석입니다. " * 30, ids[:2])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided
        note = engine.charters.load(PurePosixPath("문학"))
        assert note is not None and note.purpose == "주제: 문학"

    async def test_a_sign_carrying_a_request_local_handle_is_not_written_to_disk(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Handles mean nothing outside their request, and one reached a public file."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "D0001과 D0003을 제외한 나머지 문학 자료", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        note = engine.charters.load(PurePosixPath("문학"))
        assert note is not None and note.purpose == "주제: 문학"

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
            if p.is_dir() and p.name not in ("_inbox", ".bismuth", "아폴로")
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
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "문학/doc0.txt.md").is_file()
        assert not (engine.vault.root / "doc0.txt.md").exists()

    async def test_a_loose_document_can_reuse_an_existing_direct_sign(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        # The first two files have moved, so doc2 is D0001 in this new request-local
        # view rather than its earlier D0003. Routing is one closed F### choice per
        # loose document (ADR-0014).
        script.set_routes({"D0001": "F001"})
        # Nothing new emerges, which is what leaves the loose document to routing: the
        # pile is read for a new class first, and only what no class wanted is offered
        # to the signs already standing.
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided[0].created == (PurePosixPath("문학"),)
        assert divided[0].moved == 1
        assert (engine.vault.root / "문학/doc2.txt").is_file()
        assert (engine.vault.root / "문학/doc2.txt.md").is_file()
        assert (engine.vault.root / "doc3.txt").is_file()

    async def test_the_new_folder_gets_a_note_that_distinguishes_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "소설과 시. 과학 자료가 아닌 것.", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath("문학"))
        assert charter is not None
        assert charter.purpose == "소설과 시. 과학 자료가 아닌 것."

    async def test_drawing_a_class_out_is_one_undoable_batch(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])
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
        assert charter.split_basis == "주제"  # the axis, not this one extraction
        assert charter.split_at_documents == 4
        assert charter.divided


class TestReview:
    async def test_large_review_is_complete_but_context_isolated(
        self,
        engine: Bismuth,
        llm,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        # Comfortably above the system prompt, which is most of a maintenance call. At
        # 8,000 the test stopped exercising packeting and started tripping on
        # prompt length -- a wording change made one sign alone exceed the budget.
        monkeypatch.setattr(subdivision_service, "MAX_MAINTENANCE_PROMPT_CHARS", 16_000)
        documents = [
            (f"D{index:04d}", f"current=x/{index}.txt | " + "가" * 1_300) for index in range(1, 9)
        ]
        charter = Charter(
            path=PurePosixPath(),
            title="/",
            purpose="문서",
            split_basis="문서 종류",
            split_question="이 문서의 종류는 무엇인가?",
            split_at_documents=4,
        )

        review = await engine.maintenance._review_boundary(  # type: ignore[attr-defined]
            folder=PurePosixPath(),
            purpose=charter.purpose,
            charter=charter,
            total=len(documents),
            documents=documents,
            children=[("자료", "자료 문서"), ("기록", "기록 문서")],
        )

        calls = _review_prompts(llm)
        assert review.holds
        assert len(calls) > 1
        assert all(len(prompt.system) + len(prompt.user) <= 16_000 for prompt in calls)
        # Every document is presented; each packet is now asked once per check, so a
        # handle appears once per check rather than once in total.
        seen = {handle for prompt in calls for handle in re.findall(r"\[(D\d{4})\]", prompt.user)}
        assert seen == {document_id for document_id, _ in documents}

    async def test_wide_review_reads_every_direct_sign_in_bounded_packets(
        self,
        engine: Bismuth,
        llm,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        # Comfortably above the system prompt, which is most of a maintenance call. At
        # 8,000 the test stopped exercising packeting and started tripping on
        # prompt length -- a wording change made one sign alone exceed the budget.
        monkeypatch.setattr(subdivision_service, "MAX_MAINTENANCE_PROMPT_CHARS", 16_000)
        children = [(f"분류-{index:03d}", "범위 " + "다" * 180) for index in range(40)]
        charter = Charter(
            path=PurePosixPath(),
            title="/",
            purpose="문서",
            split_basis="문서 종류",
            split_question="이 문서의 종류는 무엇인가?",
            split_at_documents=1,
        )

        review = await engine.maintenance._review_boundary(  # type: ignore[attr-defined]
            folder=PurePosixPath(),
            purpose=charter.purpose,
            charter=charter,
            total=1,
            documents=[("D0001", "current=분류-000/문서.txt | 문서")],
            children=children,
        )

        calls = _review_prompts(llm)
        assert review.holds
        assert len(calls) > 1
        assert all(len(prompt.system) + len(prompt.user) <= 16_000 for prompt in calls)
        combined = "\n".join(prompt.user for prompt in calls)
        assert all(name in combined for name, _ in children)

    async def test_large_replacement_assigns_every_document_in_bounded_packets(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        # Comfortably above the system prompt, which is most of a maintenance call. At
        # 8,000 the test stopped exercising packeting and started tripping on
        # prompt length -- a wording change made one sign alone exceed the budget.
        monkeypatch.setattr(subdivision_service, "MAX_MAINTENANCE_PROMPT_CHARS", 16_000)
        documents = [
            (f"D{index:04d}", f"current=x/{index}.txt | " + "나" * 1_300) for index in range(1, 9)
        ]
        charter = Charter(
            path=PurePosixPath(),
            title="/",
            purpose="문서",
            split_basis="기존 축",
            split_question="기존 값은 무엇인가?",
            split_at_documents=4,
        )

        # Assignment is one closed G### choice per document (ADR-0014), so an oversized
        # subtree costs more calls rather than one reply that can drop a document.
        script.set_assignments({document_id: "G001" for document_id, _ in documents})
        replacement = await engine.maintenance._propose_replacement(  # type: ignore[attr-defined]
            folder=PurePosixPath(),
            purpose=charter.purpose,
            charter=charter,
            total=len(documents),
            documents=documents,
            children=[("옛 분류", "기존 문서")],
        )

        assert replacement is not None
        assigned = [
            document_id for group in replacement.groups for document_id in group.document_ids
        ]
        assert sorted(assigned) == sorted(document_id for document_id, _ in documents)
        assignment_calls = [prompt for prompt in llm.prompts_for(None) if "\n  [G" in prompt.user]
        assert len(assignment_calls) == len(documents)
        assert all(len(prompt.system) + len(prompt.user) <= 8_000 for prompt in assignment_calls)

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
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])
        await engine.subdivision.consider(PurePosixPath())  # divided at 4
        llm.calls.clear()

        script.set(placement_prompts.PlacementDecision, place_at(""))
        for index in range(4):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")

        assert _review_prompts(llm)

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
        ids = await _fill(engine, script, 3)
        _emerges(script, "문학", "문학", ids[:2])
        await engine.subdivision.consider(PurePosixPath())  # divided, recorded at 3

        script.set(placement_prompts.PlacementDecision, place_at(""))
        for index in range(4):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")

        charter = engine.charters.load(PurePosixPath())
        assert charter is not None
        assert charter.split_at_documents > 3  # moved up with the holding review
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())
        assert not _review_prompts(llm)  # not due again yet

    async def test_a_holding_review_still_lets_a_new_class_come_out(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """The two questions are different jobs. "The old split still holds" must not
        answer "has anything new grown here", or the folder freezes at the size it was
        first divided at -- measured: nineteen of twenty-nine documents stranded."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])
        await engine.subdivision.consider(PurePosixPath())

        script.set(placement_prompts.PlacementDecision, place_at(""))
        # Nothing emerges while the pile builds. Left scripted, "문학" would be proposed
        # again, and naming a shelf that already stands here now routes the loose
        # documents behind it instead of being refused.
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))
        # Four, not three: two go to the second child and two to the emerging class,
        # and one has to be left over or the class takes the whole pile, which is a
        # move down a level rather than a division.
        for index in range(4):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")

        # A second child, because one child is a provisional shelf and is never reviewed
        # as a complete boundary (ADR-0014). Without it there is no review to hold.
        _emerges(script, "역사", "역사", ["D0001", "D0002"])
        await engine.subdivision.consider(PurePosixPath())

        # Force the root well past its doubling so the review certainly runs first.
        root = engine.charters.load(PurePosixPath())
        assert root is not None
        (engine.vault.root / "_folder.md").write_text(
            root.model_copy(update={"split_at_documents": 1}).to_markdown(), encoding="utf-8"
        )

        # doc0/doc1 went into 문학; these two are still loose at the root.
        # 문학 and 역사 took four; the handles renumber over what is still loose.
        _emerges(script, "과학", "과학 자료", ["D0001", "D0002"])
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        assert _review_prompts(llm)  # the review did run
        assert (engine.vault.root / "과학").is_dir()  # and did not swallow the other question

    async def test_a_holding_review_moves_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        before = sorted(p.name for p in engine.vault.root.iterdir())

        # The scripted Review holds by default.
        for index in range(4):
            await add(engine, f"more{index}.txt", f"추가 문서 {index}")

        assert (engine.vault.root / "문학").is_dir()
        assert "문학" in before

    async def test_a_failed_migration_audit_enters_complete_replacement(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        # Two children, because one is a provisional shelf and is never reviewed as a
        # complete boundary (ADR-0014).
        _emerges(script, "역사", "역사 자료", ["D0001"])
        await engine.subdivision.consider(PurePosixPath())

        note_path = engine.vault.root / "_folder.md"
        note_path.write_text(
            # Written against the live version: pinning the old number made this a
            # no-op replace every time the schema moved, and the migration path it is
            # supposed to exercise stopped running silently.
            note_path.read_text(encoding="utf-8").replace(
                f"bismuth_charter: {CHARTER_SCHEMA_VERSION}", "bismuth_charter: 4", 1
            ),
            encoding="utf-8",
        )
        # The legacy boundary fails its audit; the replacement proposed after it passes.
        asked = 0

        def audit(prompt, schema):  # type: ignore[no-untyped-def]
            nonlocal asked
            first = asked == 0
            asked += 1
            return subdivision_prompts.BoundaryAudit(
                one_property=not first,
                names_answer_question=not first,
                mutually_exclusive=not first,
                useful_for_navigation=not first,
            )

        script.set(subdivision_prompts.BoundaryAudit, audit)
        _replacement(
            script,
            basis="자료 속성",
            question="어느 자료 속성값에 해당하는가?",
            groups=[
                _group("첫째 값", "첫째 값의 문서", ["D0001", "D0002"]),
                _group("둘째 값", "둘째 값의 문서", ["D0003", "D0004"]),
            ],
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided[0].moved == 4
        assert (engine.vault.root / "첫째 값").is_dir()
        assert (engine.vault.root / "둘째 값").is_dir()

    async def test_a_review_replaces_the_whole_boundary_and_reuses_names(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        waiting = engine.vault.root / "_inbox" / "unreadable.bin"
        waiting.write_bytes(b"not part of the organised collection")

        root = engine.charters.load(PurePosixPath())
        assert root is not None
        (engine.vault.root / "_folder.md").write_text(
            root.model_copy(update={"split_at_documents": 1}).to_markdown(), encoding="utf-8"
        )
        script.set(
            subdivision_prompts.Review,
            subdivision_prompts.Review(
                one_axis=False,
                coherent_membership=False,
                useful_navigation=False,
            ),
        )
        _replacement(
            script,
            basis="자료 속성",
            question="어느 자료 속성값에 해당하는가?",
            groups=[
                _group("문학", "첫 번째 값", ["D0001", "D0002", "D0003"]),
                _group("다른 값", "두 번째 값", ["D0004", "D0005", "D0006"]),
            ],
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided[0].moved == 6
        assert len(list((engine.vault.root / "문학").glob("*.txt"))) == 3
        assert len(list((engine.vault.root / "다른 값").glob("*.txt"))) == 3
        assert not list(engine.vault.root.glob("*.txt"))
        assert waiting.is_file()
        literature = engine.charters.load(PurePosixPath("문학"))
        other = engine.charters.load(PurePosixPath("다른 값"))
        assert literature is not None and literature.purpose == "첫 번째 값"
        assert other is not None and other.purpose == "두 번째 값"
        updated = engine.charters.load(PurePosixPath())
        assert updated is not None
        assert updated.split_basis == "자료 속성"

    async def test_a_valid_alternative_does_not_replace_a_learned_boundary_without_gain(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        root = engine.charters.load(PurePosixPath())
        assert root is not None
        (engine.vault.root / "_folder.md").write_text(
            root.model_copy(update={"split_at_documents": 1}).to_markdown(), encoding="utf-8"
        )
        script.set(
            subdivision_prompts.Review,
            subdivision_prompts.Review(
                one_axis=False, coherent_membership=False, useful_navigation=False
            ),
        )
        _replacement(
            script,
            basis="다른 속성",
            question="어느 값인가?",
            groups=[
                _group("첫 값", "첫 값 문서", ["D0001", "D0002", "D0003"]),
                _group("둘째 값", "둘째 값 문서", ["D0004", "D0005", "D0006"]),
            ],
        )
        script.set(
            subdivision_prompts.ReplacementAudit,
            subdivision_prompts.ReplacementAudit(
                fixes_observed_failure=True,
                better_navigation=False,
            ),
        )
        # The refused replacement is what this test is about. Left scripted, "문학" would
        # be proposed again afterwards and would route the loose documents behind it.
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert (engine.vault.root / "문학").is_dir()
        assert not (engine.vault.root / "첫 값").exists()

    async def test_a_semantically_invalid_boundary_is_rejected(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "value a", "first candidate", ids[:2], axis="property a vs property b")
        script.set(
            subdivision_prompts.BoundaryAudit,
            subdivision_prompts.BoundaryAudit(
                one_property=False,
                names_answer_question=False,
                mutually_exclusive=True,
                useful_for_navigation=False,
                notes_are_routing_signs=True,
            ),
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert not (engine.vault.root / "value a").exists()


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


class TestOneAxisPerFolder:
    """Sub-folders of one folder are answers to one question. Without that, siblings sit
    on different distinctions and no name rules anything out -- measured on 300 legal
    documents as 주제 (과학기술), 문서 종류 (시행규칙) and individual statute names side by
    side at the same root."""

    async def test_the_axis_is_recorded_when_the_first_class_comes_out(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2], axis="주제 분야")

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath())
        assert charter is not None
        assert charter.split_basis == "주제 분야"

    async def test_a_divided_folder_is_held_to_the_axis_it_has(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2], axis="주제 분야")
        await engine.subdivision.consider(PurePosixPath())
        llm.calls.clear()

        _emerges(script, "과학", "과학 자료", ids[2:], axis="완전히 다른 축")
        await engine.subdivision.consider(PurePosixPath())

        # The second look is told the axis rather than asked for one...
        asked = llm.prompts_for(subdivision_prompts.Emerging)[-1]
        assert "주제 분야" in asked.user
        # ...and the folder keeps it, whatever the reply says.
        charter = engine.charters.load(PurePosixPath())
        assert charter is not None
        assert charter.split_basis == "주제 분야"

    async def test_redrawing_a_note_does_not_erase_the_axis(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Notes are redrawn every time a document lands, and before subdivision runs.
        A redraft that dropped the history erased the axis on the way in, so it survived
        only until the next arrival -- which is most of why sibling folders drifted."""
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2], axis="주제 분야")
        await engine.subdivision.consider(PurePosixPath())
        before = engine.charters.load(PurePosixPath("문학"))
        assert before is not None

        script.set(placement_prompts.PlacementDecision, place_at("문학"))
        await add(engine, "another.txt", "문학 문서 하나 더")

        after = engine.charters.load(PurePosixPath("문학"))
        assert after is not None
        assert after.split_basis == before.split_basis
        assert after.split_at_documents == before.split_at_documents


class TestAnAxisIsSpentOnce:
    """Within a folder, every ancestor's axis has one constant value -- that is what put
    these documents together -- so none of them can separate anything here. Measured
    without this: 시행규칙/과학기술정보통신부 소관/시행규칙, and beside it 시행령, which reads
    as though an enforcement rule could contain an enforcement decree."""

    async def _divide_root(self, engine: Bismuth, script: ScriptedModel) -> list[str]:
        ids = await _fill(engine, script, 6)
        _emerges(script, "법률", "법률", ids[:4], axis="법령의 종류")
        await engine.subdivision.consider(PurePosixPath())
        return ids

    async def test_an_axis_used_above_is_refused(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await self._divide_root(engine, script)
        script.set(placement_prompts.PlacementDecision, place_at("법률"))
        await add(engine, "more.txt", "법률 문서 하나 더")

        # The child proposes the axis its parent was already divided along.
        _emerges(script, "시행령", "시행령", ids[:1], axis="법령의 종류")
        divided = await engine.subdivision.consider(PurePosixPath("법률"))

        assert divided == []
        assert not (engine.vault.root / "법률/시행령").exists()

    async def test_a_different_axis_below_is_fine(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await self._divide_root(engine, script)

        _emerges(script, "금융위원회", "금융위원회 소관", ids[:2], axis="소관 부처")
        divided = await engine.subdivision.consider(PurePosixPath("법률"))

        assert (engine.vault.root / "법률/금융위원회").is_dir()
        assert divided[0].basis == "소관 부처"

    async def test_a_class_may_not_carry_a_grandparents_name(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await self._divide_root(engine, script)
        _emerges(script, "금융위원회", "금융위원회 소관", ids[:2], axis="소관 부처")
        await engine.subdivision.consider(PurePosixPath("법률"))

        # Two levels down, proposing the name of the top-level folder.
        _emerges(script, "법률", "법률", ids[:1], axis="문서 성격")
        divided = await engine.subdivision.consider(PurePosixPath("법률/금융위원회"))

        assert divided == []
        assert not (engine.vault.root / "법률/금융위원회/법률").exists()


class TestTheAxisStaysSingleLine:
    def test_multiline_state_is_not_an_axis(self) -> None:
        with pytest.raises(ValidationError):
            subdivision_prompts.Replacement(basis="one\ntwo")
        with pytest.raises(ValidationError):
            subdivision_prompts.Emerging(emerged=True, axis="one\ntwo", name="x")


class TestABoundaryThatIsNotCarrying:
    """SPEC 3.3.1: a folder's loose pile must not outweigh its largest child.

    It used to open the review, and the review's only remedy is to redraw the boundary --
    which moves the documents that were already filed and leaves the pile untouched.
    An undivided pile is answered by growing a class out of it, which every arrival asks
    for anyway, so the review stays on its schedule.
    """

    async def test_an_undivided_pile_does_not_trigger_a_redraw(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        script.set(placement_prompts.PlacementDecision, place_at(""))
        llm.calls.clear()

        await add(engine, "more.txt", "추가 문서")

        assert not _review_prompts(llm)


class TestAFolderBornFull:
    async def test_a_new_shelf_is_asked_about_itself_once(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """Its documents were moved in, not filed in, so nothing else would ask it."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학", ids[:4])
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        asked = [p for p in llm.prompts_for(subdivision_prompts.Emerging) if "문학" in p.user]
        assert asked


class TestStandingFoldersTogether:
    """The fourth operation: a level that grew too wide can be narrowed again."""

    async def _three_shelves(self, engine: Bismuth, script: ScriptedModel) -> None:
        """Handles are request-local, so the two documents still loose are always D0001-2."""
        for name in ("문학", "과학", "역사"):
            _emerges(script, name, f"{name} 자료", ["D0001", "D0002"])
            await engine.subdivision.consider(PurePosixPath())

    async def test_several_folders_move_under_one_broader_name(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="인문", sign="문학과 역사"),
        )
        script.set_shelved(["문학", "역사"])

        await self._three_shelves(engine, script)

        assert (engine.vault.root / "인문/문학").is_dir()
        assert (engine.vault.root / "인문/역사").is_dir()
        # No document changed the folder it is in; only the path above it changed.
        assert (engine.vault.root / "인문/문학/doc0.txt").is_file()
        assert not (engine.vault.root / "문학").exists()
        # And something stayed behind, or this was a rename.
        assert (engine.vault.root / "과학").is_dir()

    async def test_a_shelf_that_would_take_every_folder_is_refused(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="자료", sign="모든 자료"),
        )
        # 아폴로 is the seeded fixture folder, so this really is every folder here.
        script.set_shelved(["문학", "과학", "역사", "아폴로"])

        await self._three_shelves(engine, script)

        assert not (engine.vault.root / "자료").exists()
        assert (engine.vault.root / "문학").is_dir()

    async def test_standing_folders_together_is_one_undoable_batch(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await _fill(engine, script, 8)
        script.set(
            subdivision_prompts.Grouping,
            subdivision_prompts.Grouping(emerged=True, name="인문", sign="문학과 역사"),
        )
        script.set_shelved(["문학", "역사"])
        await self._three_shelves(engine, script)

        entry = next(e for e in engine.journal.iter_entries() if "group" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "문학/doc0.txt").is_file()
        assert not (engine.vault.root / "인문").exists()


class TestNamingAShelfThatAlreadyStands:
    async def test_the_loose_documents_go_behind_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Not a mistake: an answer to a different question, and it used to be thrown
        away -- 119 times at one root, which is why 114 documents stayed loose."""
        await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ["D0001", "D0002"])
        await engine.subdivision.consider(PurePosixPath())
        assert (engine.vault.root / "문학/doc0.txt").is_file()

        # The same name again, over documents that are still loose.
        _emerges(script, "문학", "문학 자료", ["D0001", "D0002"])

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "문학/doc2.txt").is_file()
        assert not (engine.vault.root / "doc2.txt").exists()


class TestWhereTheDocumentsWent:
    """Descending is decided by whether a shelf emptied its parent, not by depth."""

    async def test_a_thin_shelf_is_not_descended_into(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """It left a pile behind, and the pile is the more urgent question."""
        await _fill(engine, script, 8)
        _emerges(script, "문학", "문학 자료", ["D0001", "D0002"])
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "문학").is_dir()
        assert not [p for p in llm.prompts_for(subdivision_prompts.Emerging) if "문학/" in p.user]

    async def test_a_shelf_that_emptied_its_parent_is_asked_again(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,  # type: ignore[no-untyped-def]
    ) -> None:
        """The whole problem moved one level down; nothing else would ask it."""
        await _fill(engine, script, 8)
        _emerges(script, "문학", "문학 자료", [f"D{index:04d}" for index in range(1, 7)])
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        asked = [
            p for p in llm.prompts_for(subdivision_prompts.Emerging) if "FOLDER: 문학" in p.user
        ]
        assert asked


class TestARefusedSignSaysWhy:
    async def test_the_fallback_note_is_recorded_with_its_reason(
        self, engine: Bismuth, script: ScriptedModel, caplog
    ) -> None:
        """The fallback repeats the folder name and rules nothing out, so a run that
        writes it often has a defect -- and only the finished vault showed it before."""
        ids = await _fill(engine, script, 6)
        # A sign that is the folder name again is one of the four refusals.
        _emerges(script, "문학", "문학", ids[:2])

        events = []
        with mock.patch.object(
            subdivision_service, "log_trace", lambda e, **f: events.append((e, f))
        ):
            await engine.subdivision.consider(PurePosixPath())

        refusals = [f for e, f in events if e == "subdivide.sign_refused"]
        assert refusals
        assert refusals[0]["reason"] == "sign is the folder name again"
        assert refusals[0]["name"] == "문학"


class TestAReplacementNeedNotTakeEverything:
    async def test_a_document_that_fits_no_new_sign_stays_where_it_is(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Offered only the new signs, a model has to pick one and picks the broadest.
        Measured: a root redrawn on a finance axis pulled 중대재해처벌법 and
        국립공업고등학교 설치령 behind 금융산업 구조개선."""
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        root = engine.charters.load(PurePosixPath())
        assert root is not None
        (engine.vault.root / "_folder.md").write_text(
            root.model_copy(update={"split_at_documents": 1}).to_markdown(), encoding="utf-8"
        )
        script.set(
            subdivision_prompts.Review,
            subdivision_prompts.Review(
                one_axis=False, coherent_membership=False, useful_navigation=False
            ),
        )
        _replacement(
            script,
            basis="다른 속성",
            question="어느 값인가?",
            groups=[
                _group("첫 값", "첫 값 문서", ["D0001", "D0002"]),
                _group("둘째 값", "둘째 값 문서", ["D0003", "D0004"]),
            ],
        )
        # The rest fit neither sign.
        script.set_assignments({"D0001": "G001", "D0002": "G001", "D0003": "G002", "D0004": "G002"})
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))

        await engine.subdivision.consider(PurePosixPath())

        # Handles renumber over the reviewed subtree, so which document stays is not
        # fixed here; what matters is that the new boundary did not have to swallow all
        # six to be applied.
        taken = sum(
            1
            for name in ("첫 값", "둘째 값")
            if (engine.vault.root / name).is_dir()
            for _ in (engine.vault.root / name).glob("*.txt")
        )
        assert taken < 6, "a replacement must be allowed to leave documents where they are"


class TestNoDocumentIsLeftStaged:
    async def test_a_document_no_class_claimed_comes_back_to_the_folder(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Replacement stages every document and only unstages what a group claimed.
        When it had to account for all of them that was safe; once it could leave some
        behind, those stayed under .bismuth -- eight of a hundred, invisible to the
        vault and to whoever owned them."""
        await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ["D0001", "D0002"])
        await engine.subdivision.consider(PurePosixPath())
        root = engine.charters.load(PurePosixPath())
        assert root is not None
        (engine.vault.root / "_folder.md").write_text(
            root.model_copy(update={"split_at_documents": 1}).to_markdown(), encoding="utf-8"
        )
        script.set(
            subdivision_prompts.Review,
            subdivision_prompts.Review(
                one_axis=False, coherent_membership=False, useful_navigation=False
            ),
        )
        _replacement(
            script,
            basis="다른 속성",
            question="어느 값인가?",
            groups=[
                _group("첫 값", "첫 값 문서", ["D0001", "D0002"]),
                _group("둘째 값", "둘째 값 문서", ["D0003", "D0004"]),
            ],
        )
        script.set_assignments({"D0001": "G001", "D0002": "G001", "D0003": "G002", "D0004": "G002"})
        script.set(subdivision_prompts.Emerging, subdivision_prompts.Emerging(emerged=False))

        await engine.subdivision.consider(PurePosixPath())

        stray = list((engine.vault.root / ".bismuth").rglob("*.txt"))
        assert not stray, f"documents stranded in staging: {[p.name for p in stray]}"
        filed = list(engine.vault.root.rglob("*.txt"))
        assert len(filed) == 6, "every document must still be somewhere the vault can see"
