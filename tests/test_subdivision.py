"""Drawing a class out of a folder: when it happens, what it moves, what it leaves."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from bismuth.container import Bismuth
from bismuth.domain.charter import Charter
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import subdivision as subdivision_prompts
from bismuth.services.legacy.subdivision import evaluation as subdivision_evaluation
from bismuth.services.legacy.subdivision import helpers as subdivision_helpers
from bismuth.services.legacy.subdivision.models import _Contents
from bismuth.services.legacy.subdivision.service import _root_normalization_is_grounded
from tests.conftest import ScriptedModel
from tests.test_ingest import add, place_at


def _group(name: str, note: str, ids: list[str]) -> subdivision_prompts.Group:
    return subdivision_prompts.Group(name=name, note=note, document_ids=ids)


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
            note=note,
        ),
    )
    script.set(
        subdivision_prompts.Members,
        subdivision_prompts.Members(document_ids=ids),
    )
    script.set(
        subdivision_prompts.NormalizedSign,
        subdivision_prompts.NormalizedSign(name=name, valid=True),
    )


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
                subdivision_prompts.ReplacementSign(name=group.name, note=group.note)
                for group in groups
            ],
        ),
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
    def test_root_normalization_must_retain_a_source_label_anchor(self) -> None:
        assert _root_normalization_is_grounded("기업 지원", "중소기업")
        assert _root_normalization_is_grounded("금융 규제", "금융")
        assert _root_normalization_is_grounded("기업 거래 및 상생협력", "거래 공정화")
        assert not _root_normalization_is_grounded("소비자 보호", "과학기술")
        assert not _root_normalization_is_grounded("방송통신", "법령")
        assert not _root_normalization_is_grounded(
            "산업/기술 지원", "과학기술정보통신부 소관 법령"
        )

    def test_family_validation_resolves_request_handles_through_paths(
        self, engine: Bismuth, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        act = PurePosixPath("하도급거래 공정화에 관한 법률(법률).pdf")
        decree = PurePosixPath("하도급거래 공정화에 관한 법률 시행령(대통령령).pdf")
        cards = {
            act: SimpleNamespace(
                title="하도급거래 공정화에 관한 법률", topics=[], doc_type="법률"
            ),
            decree: SimpleNamespace(
                title="하도급거래 공정화에 관한 법률 시행령", topics=[], doc_type="대통령령"
            ),
        }
        monkeypatch.setattr(engine.maintenance, "_card_of", cards.get)
        contents = _Contents(
            documents=[("D0001", "법률", act), ("D0002", "시행령", decree)]
        )

        coherent = engine.maintenance._cohere_families(  # type: ignore[attr-defined]
            contents,
            [
                _group("법률", "법률", ["D0001"]),
                _group("대통령령", "대통령령", ["D0002"]),
            ],
        )

        assert coherent is None
        assert (
            engine.maintenance._independent_family_units(  # type: ignore[attr-defined]
                contents, {"D0001", "D0002"}
            )
            == 1
        )

        problem = engine.maintenance._explicit_card_value_problem(  # type: ignore[attr-defined]
            contents,
            [_group("대통령령", "대통령령", ["D0001", "D0002"])],
        )
        assert problem is not None and "D0001" in problem

    def test_document_type_partition_is_rejected_from_membership(
        self, engine: Bismuth, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = [PurePosixPath(f"doc{index}.pdf") for index in range(4)]
        cards = {
            paths[0]: SimpleNamespace(title="가", topics=[], doc_type="법률"),
            paths[1]: SimpleNamespace(title="나", topics=[], doc_type="법률"),
            paths[2]: SimpleNamespace(title="다 시행령", topics=[], doc_type="대통령령"),
            paths[3]: SimpleNamespace(title="라 시행령", topics=[], doc_type="대통령령"),
        }
        monkeypatch.setattr(engine.maintenance, "_card_of", cards.get)
        contents = _Contents(
            documents=[(f"D{index + 1:04d}", "문서", path) for index, path in enumerate(paths)]
        )

        problem = engine.maintenance._explicit_card_value_problem(  # type: ignore[attr-defined]
            contents,
            [
                _group("상위 규범", "상위 규범", ["D0001", "D0002"]),
                _group("하위 규범", "하위 규범", ["D0003", "D0004"]),
            ],
        )

        assert problem == "proposed siblings partition documents by card doc_type metadata"

    def test_metadata_sketch_is_rejected_before_assignment(
        self, engine: Bismuth, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = [PurePosixPath("law.pdf"), PurePosixPath("decree.pdf")]
        cards = {
            paths[0]: SimpleNamespace(title="소비자 보호에 관한 법률", topics=[], doc_type="법률"),
            paths[1]: SimpleNamespace(title="보험업법 시행령", topics=[], doc_type="대통령령"),
        }
        monkeypatch.setattr(engine.maintenance, "_card_of", cards.get)
        contents = _Contents(
            documents=[("D0001", "문서", paths[0]), ("D0002", "문서", paths[1])]
        )

        metadata = subdivision_prompts.InitialBoundarySketch(
            basis="legal hierarchy",
            basis_question="Which legal form is this?",
            signs=[
                subdivision_prompts.ReplacementSign(name="법 (Act)"),
                subdivision_prompts.ReplacementSign(name="시행령 (Enforcement Decree)"),
            ],
        )
        semantic = subdivision_prompts.InitialBoundarySketch(
            basis="정책 분야",
            basis_question="어느 정책 분야인가?",
            signs=[
                subdivision_prompts.ReplacementSign(name="소비자 보호"),
                subdivision_prompts.ReplacementSign(name="보험"),
            ],
        )

        assert engine.maintenance._sketch_uses_card_metadata_facet(contents, metadata)  # type: ignore[attr-defined]
        assert not engine.maintenance._sketch_uses_card_metadata_facet(contents, semantic)  # type: ignore[attr-defined]

    def test_existing_axis_prompt_carries_the_immutable_question(self) -> None:
        prompt = subdivision_prompts.build_emerging(
            path="",
            purpose="법령",
            documents=[("D0001", "금융 문서")],
            children=[("기존", "기존 표지")],
            axis="소관 부처",
            axis_question="이 문서의 소관 부처는 어디인가?",
        )

        assert "THE RECORDED AXIS" in prompt.user
        assert "THE RECORDED QUESTION" in prompt.user
        assert "이 문서의 소관 부처는 어디인가?" in prompt.user

    async def test_model_prompts_use_request_local_handles_not_catalog_hashes(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        await _fill(engine, script, 4)

        prompts = llm.prompts_for(subdivision_prompts.Emerging)
        assert prompts
        assert all(not re.search(r"\[[0-9a-f]{16}(?:~\d+)?\]", item.user) for item in prompts)
        assert "[D0001]" in prompts[-1].user

    async def test_id_returning_calls_are_packeted_by_output_cardinality(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        documents = [(f"D{index:04d}", f"문서 {index}") for index in range(1, 31)]
        script.set(
            subdivision_prompts.Members,
            lambda prompt, schema: subdivision_prompts.Members(
                document_ids=re.findall(r"\[(D\d{4})\]", prompt.user)
            ),
        )

        result = await engine.maintenance._find_members(  # type: ignore[attr-defined]
            folder=PurePosixPath(),
            purpose="자료",
            documents=documents,
            children=[],
            name="자료",
            note="자료 문서",
        )

        calls = llm.prompts_for(subdivision_prompts.Members)
        assert len(calls) == 3
        assert all(len(re.findall(r"\[(D\d{4})\]", call.user)) <= 12 for call in calls)
        assert result.document_ids == [item[0] for item in documents]

    async def test_nothing_happens_when_nothing_has_gathered(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """The scripted default is "nothing emerged"; a young folder stays flat."""
        await _fill(engine, script, 4)

        assert not (engine.vault.root / "문학").exists()
        assert (engine.vault.root / "doc0.txt").is_file()

    async def test_declined_initial_boundary_waits_for_materially_new_evidence(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        del script, llm
        folder = PurePosixPath()
        assert engine.subdivision._initial_boundary_due(folder, 3)  # type: ignore[attr-defined]
        engine.subdivision._initial_boundary_attempt_at[folder] = 3  # type: ignore[attr-defined]
        assert not engine.subdivision._initial_boundary_due(folder, 4)  # type: ignore[attr-defined]
        assert not engine.subdivision._initial_boundary_due(folder, 6)  # type: ignore[attr-defined]
        assert engine.subdivision._initial_boundary_due(folder, 7)  # type: ignore[attr-defined]

    async def test_recent_failed_candidate_is_exposed_until_evidence_doubles(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        folder = PurePosixPath()
        engine.subdivision._remember_candidate(  # type: ignore[attr-defined]
            folder, "산업 안전", documents=10
        )

        assert engine.subdivision._recent_candidates(folder, 14) == [  # type: ignore[attr-defined]
            "산업 안전"
        ]
        assert engine.subdivision._recent_candidates(folder, 20) == []  # type: ignore[attr-defined]

        await _fill(engine, script, 4)
        await engine.subdivision._find_emerging(  # type: ignore[attr-defined]
            folder=folder,
            purpose="",
            documents=[("D0001", "주제")],
            children=[],
            axis="",
            axis_question="",
            spent=[],
            recently_rejected=["산업 안전"],
        )
        prompt = llm.prompts_for(subdivision_prompts.Emerging)[-1]
        assert "산업 안전" in prompt.user
        assert "Do not repeat or paraphrase" in prompt.user

    async def test_finalize_keeps_drawing_classes_after_a_last_item_success(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])
        first = await engine.subdivision.consider(PurePosixPath())
        assert first

        # No new document arrives after the first class. Finalization must still ask
        # the reduced root and can draw another independently validated class.
        _emerges(script, "과학", "과학 자료", ids[2:4])
        finalized = await engine.subdivision.finalize_pending()

        assert any(result.folder == PurePosixPath() for result in finalized)
        assert (engine.vault.root / "과학").is_dir()

    async def test_final_recovery_routes_only_a_closed_verified_existing_sign(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        charter = engine.charters.load(PurePosixPath())
        contents = engine.subdivision._read(PurePosixPath())  # type: ignore[attr-defined]
        assert charter is not None
        assert charter.divided, charter
        assert charter.split_basis and charter.split_question
        assert contents.children and contents.documents

        # The closed chooser may name only a displayed handle or STAY. The scripted
        # membership auditor independently returns BELONG for every selected member.
        script.set_choice(
            lambda prompt, schema: (
                "F001"
                if prompt.system.startswith("Route this one loose document")
                else "STAY"
            )
        )
        recovered = await engine.subdivision._route_verified_existing(  # type: ignore[attr-defined]
            PurePosixPath()
        )

        assert recovered is not None and recovered.happened
        assert not any((engine.vault.root / f"doc{index}.txt").exists() for index in range(2, 6))
        assert all(
            (engine.vault.root / "문학" / f"doc{index}.txt").is_file()
            for index in range(6)
        )

    async def test_rejected_family_is_not_retried_under_the_same_topology(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        script.set_choice("F001")
        script.membership_choice = "STAY"
        attempts: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()

        first = await engine.subdivision._route_verified_existing(  # type: ignore[attr-defined]
            PurePosixPath(), attempted=attempts
        )
        calls_after_first = len(llm.prompts_for(None))
        second = await engine.subdivision._route_verified_existing(  # type: ignore[attr-defined]
            PurePosixPath(), attempted=attempts
        )

        assert first is None and second is None
        assert len(llm.prompts_for(None)) == calls_after_first

    async def test_final_rebalance_only_reviews_this_upload_against_all_siblings(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 7)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        _emerges(script, "과학", "과학 자료", ids[2:4])
        await engine.subdivision.consider(PurePosixPath())

        def choose_science(prompt, schema):  # type: ignore[no-untyped-def]
            del schema
            for line in prompt.user.splitlines():
                if line.startswith("  [F") and line.endswith("과학/"):
                    return line.split("]", 1)[0].removeprefix("  [")
            return "KEEP"

        script.rebalance_choice = choose_science
        script.rebalance_comparison = "MOVE"
        result = await engine.subdivision._rebalance_focus(  # type: ignore[attr-defined]
            {"doc0.txt"}
        )

        assert result is not None and result.happened
        assert not (engine.vault.root / "문학" / "doc0.txt").exists()
        assert (engine.vault.root / "과학" / "doc0.txt").is_file()
        assert (engine.vault.root / "문학" / "doc1.txt").is_file()

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

    async def test_a_later_class_audit_is_isolated_from_existing_membership(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:
        ids = await _fill(engine, script, 6)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())

        llm.calls.clear()
        _emerges(script, "과학", "과학 자료", ids[2:4])
        await engine.subdivision.consider(PurePosixPath())

        audit_prompt = llm.prompts_for(subdivision_prompts.ClassAudit)[-1].user
        assert "과학" in audit_prompt
        assert "문학/" in audit_prompt  # sibling sign context, not old membership
        assert "EXISTING SIBLING SIGNS" in audit_prompt

    async def test_an_essay_length_folder_note_uses_the_validated_class_name(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료에 관한 상세 분석입니다. " * 30, ids[:2])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided
        note = engine.charters.load(PurePosixPath("문학"))
        assert note is not None and note.purpose == "주제 분야 → 문학"

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
        ids = await _fill(engine, script, 3)
        _emerges(script, "문학", "문학", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        assert (engine.vault.root / "문학/doc0.txt.md").is_file()
        assert not (engine.vault.root / "doc0.txt.md").exists()

    async def test_a_loose_document_is_not_bulk_rerouted_over_placement_decision(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "문학", "문학 자료", ids[:2])
        await engine.subdivision.consider(PurePosixPath())
        script.set(
            subdivision_prompts.ExistingAssignments,
            lambda prompt, schema: subdivision_prompts.ExistingAssignments(
                groups=[
                    subdivision_prompts.ExistingAssignment(
                        folder_id="F001",
                        # The first two files have moved, so doc2 is D0001 in this
                        # new request-local view rather than its earlier D0003.
                        document_ids=["D0001"],
                    )
                ]
            ),
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert (engine.vault.root / "doc2.txt").is_file()
        assert (engine.vault.root / "doc2.txt.md").is_file()
        assert (engine.vault.root / "doc3.txt").is_file()

    async def test_the_new_folder_gets_a_note_that_distinguishes_it(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 3)
        _emerges(script, "문학", "소설과 시. 과학 자료가 아닌 것.", ids[:2])

        await engine.subdivision.consider(PurePosixPath())

        charter = engine.charters.load(PurePosixPath("문학"))
        assert charter is not None
        assert charter.purpose == "주제 분야 → 문학"

    async def test_drawing_a_class_out_is_one_undoable_batch(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 3)
        _emerges(script, "문학", "문학", ids[:2])
        await engine.subdivision.consider(PurePosixPath())

        entry = next(e for e in engine.journal.iter_entries() if "divide" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "doc0.txt").is_file()
        assert (engine.vault.root / "doc1.txt").is_file()

    async def test_an_unusable_folder_name_is_skipped_not_fatal(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 3)
        _emerges(script, "...", "쓸 수 없는 이름", ids[:1])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert (engine.vault.root / "doc0.txt").is_file()

    async def test_a_comparison_is_not_a_class_sign(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 4)
        _emerges(script, "금융 vs 소상공인/거래", "비교형 이름", ids[:2])

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []


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
        assert charter.split_basis == "주제 분야"  # host-owned axis, not this extraction
        assert charter.split_at_documents == 4
        assert charter.divided


class TestReview:
    async def test_large_review_is_complete_but_context_isolated(
        self,
        engine: Bismuth,
        llm,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        monkeypatch.setattr(subdivision_evaluation, "MAX_MAINTENANCE_PROMPT_CHARS", 5_000)
        monkeypatch.setattr(subdivision_helpers, "MAX_MAINTENANCE_PROMPT_CHARS", 5_000)
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

        calls = llm.prompts_for(subdivision_prompts.Review)
        assert review.holds
        assert len(calls) > 1
        assert all(len(prompt.system) + len(prompt.user) <= 5_000 for prompt in calls)
        seen = [handle for prompt in calls for handle in re.findall(r"\[(D\d{4})\]", prompt.user)]
        assert sorted(seen) == sorted(document_id for document_id, _ in documents)

    async def test_wide_review_reads_every_direct_sign_in_bounded_packets(
        self,
        engine: Bismuth,
        llm,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        monkeypatch.setattr(subdivision_evaluation, "MAX_MAINTENANCE_PROMPT_CHARS", 5_000)
        monkeypatch.setattr(subdivision_helpers, "MAX_MAINTENANCE_PROMPT_CHARS", 5_000)
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

        calls = llm.prompts_for(subdivision_prompts.Review)
        assert review.holds
        assert len(calls) > 1
        assert all(len(prompt.system) + len(prompt.user) <= 5_000 for prompt in calls)
        combined = "\n".join(prompt.user for prompt in calls)
        assert all(name in combined for name, _ in children)

    async def test_large_replacement_assigns_every_document_in_bounded_packets(
        self,
        engine: Bismuth,
        script: ScriptedModel,
        llm,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        monkeypatch.setattr(subdivision_evaluation, "MAX_MAINTENANCE_PROMPT_CHARS", 5_000)
        monkeypatch.setattr(subdivision_helpers, "MAX_MAINTENANCE_PROMPT_CHARS", 5_000)
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

        def assign(prompt, schema):  # type: ignore[no-untyped-def]
            ids = re.findall(r"\[(D\d{4})\]", prompt.user)
            return subdivision_prompts.ReplacementAssignments(
                groups=[
                    subdivision_prompts.ReplacementAssignment(folder_id="G001", document_ids=ids)
                ]
            )

        script.set(subdivision_prompts.ReplacementAssignments, assign)
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
        packet_calls = llm.prompts_for(subdivision_prompts.ReplacementAssignments)
        assert len(packet_calls) > 1
        assert all(len(prompt.system) + len(prompt.user) <= 5_000 for prompt in packet_calls)

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
        ids = await _fill(engine, script, 3)
        _emerges(script, "문학", "문학", ids[:2])
        await engine.subdivision.consider(PurePosixPath())  # divided at 3
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
        ids = await _fill(engine, script, 3)
        _emerges(script, "문학", "문학", ids[:2])
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

        # doc0/doc1 went into 문학; these two are still loose at the root.
        _emerges(script, "과학", "과학 자료", [])
        script.set(
            subdivision_prompts.Members,
            # doc2 remains loose as D0001; the two new members follow it.
            subdivision_prompts.Members(document_ids=["D0002", "D0003"]),
        )
        llm.calls.clear()

        await engine.subdivision.consider(PurePosixPath())

        # One child is still a provisional shelf, not a complete boundary to redraw.
        assert not llm.prompts_for(subdivision_prompts.Review)
        assert (engine.vault.root / "과학").is_dir()  # and did not swallow the other question

    async def test_a_holding_review_moves_nothing(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 3)
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

        note_path = engine.vault.root / "_folder.md"
        note_path.write_text(
            note_path.read_text(encoding="utf-8").replace(
                "bismuth_charter: 7", "bismuth_charter: 4", 1
            ),
            encoding="utf-8",
        )
        audits = iter(
            (
                subdivision_prompts.BoundaryAudit(
                    one_property=False,
                    names_answer_question=False,
                    mutually_exclusive=False,
                    useful_for_navigation=False,
                    members_match_signs=False,
                    no_remainder_sign=False,
                    notes_are_routing_signs=False,
                ),
                subdivision_prompts.BoundaryAudit(
                    one_property=True,
                    names_answer_question=True,
                    mutually_exclusive=True,
                    useful_for_navigation=True,
                    members_match_signs=True,
                    no_remainder_sign=True,
                    notes_are_routing_signs=True,
                ),
            )
        )
        script.set(
            subdivision_prompts.BoundaryAudit,
            lambda prompt, schema: next(audits),
        )
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

    async def test_an_overlapping_boundary_plan_is_rejected_before_any_move(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        ids = await _fill(engine, script, 5)
        _emerges(script, "문학", "문학", ids[:2])
        await engine.subdivision.consider(PurePosixPath())

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
            basis="주제 분야",
            question="어느 주제 분야에 속하는가?",
            groups=[
                subdivision_prompts.Group(
                    name="과학", note="과학", document_ids=["D0003", "D0004"]
                ),
                subdivision_prompts.Group(
                    name="기술", note="기술", document_ids=["D0003", "D0005"]
                ),
            ],
        )

        divided = await engine.subdivision.consider(PurePosixPath())

        assert divided == []
        assert not (engine.vault.root / "과학").exists()
        assert not (engine.vault.root / "기술").exists()
        assert all((engine.vault.root / f"doc{index}.txt").exists() for index in range(2, 5))
        assert not any(entry.status.value == "failed" for entry in engine.journal.iter_entries())

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
        assert literature is not None and literature.purpose == "자료 속성 → 문학"
        assert other is not None and other.purpose == "자료 속성 → 다른 값"
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
                members_match_signs=True,
                no_remainder_sign=True,
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
        ids = await _fill(engine, script, 4)
        _emerges(script, "법률", "법률", ids[:3], axis="법령의 종류")
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

    def test_a_different_axis_below_is_fine(self) -> None:
        assert not subdivision_helpers._same_axis("소관 부처", ["법령의 종류"])

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
