"""Folder note (charter) refresh on document and subfolder changes."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.container import Bismuth
from bismuth.domain.charter import Charter, routing_purpose
from bismuth.prompts import charters as charter_prompts
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import subdivision as subdivision_prompts
from bismuth.services.subdivision import Divided
from tests.conftest import ScriptedModel
from tests.test_ingest import add, place_at


def test_managed_notes_stop_serialising_unused_examples_and_questions() -> None:
    note = Charter(
        path=PurePosixPath("archive"),
        title="archive",
        purpose="Documents belonging to this archive.",
        holds=("legacy example",),
        answers=("legacy question?",),
    ).to_markdown()

    assert "holds:" not in note
    assert "answers:" not in note
    assert "legacy example" not in note
    assert "legacy question" not in note


def test_folder_purpose_is_collapsed_to_one_short_line() -> None:
    draft = charter_prompts.CharterDraft(purpose="  Documents\nthat belong here.  ")
    assert draft.purpose == "Documents that belong here."


def test_routing_purpose_does_not_treat_character_count_as_meaning() -> None:
    assert routing_purpose("x" * 221, fallback="Reports") == "x" * 221


async def test_a_new_sign_preserves_normalised_model_output_without_retrying(
    engine: Bismuth, script: ScriptedModel, llm: FakeLLM
) -> None:
    folder = PurePosixPath("새 분류")
    child = folder / "하위 값"
    (Path(engine.vault.root) / "새 분류/하위 값").mkdir(parents=True)
    child_note = Charter(path=child, title=child.name, purpose="하위 값의 문서")
    (Path(engine.vault.root) / "새 분류/하위 값/_folder.md").write_text(
        child_note.to_markdown(), encoding="utf-8"
    )
    script.set(charter_prompts.CharterDraft, charter_prompts.CharterDraft(purpose="가" * 500))

    operations = await engine.charters.refresh_operations([folder])

    assert len(operations) == 1
    generated = Charter.from_markdown(operations[0][1].decode("utf-8"), path=folder)
    assert generated.purpose == "가" * 500
    assert len(_notes_for(llm, str(folder))) == 1


def _notes_for(llm: FakeLLM, folder: str) -> list[str]:
    """The user-prompt text of every note draft aimed at ``folder``, in order."""
    return [
        p.user
        for p in llm.prompts_for(charter_prompts.CharterDraft)
        if p.user.startswith(f"FOLDER: {folder}\n")
    ]


class TestDocumentChanges:
    async def test_filing_into_an_existing_folder_preserves_its_boundary_sign(
        self, engine: Bismuth, llm: FakeLLM, script: ScriptedModel
    ) -> None:
        before = engine.charters.load(PurePosixPath("아폴로/2023"))
        await add(engine, "first.txt", "아폴로 계약 A")
        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023"))
        await add(engine, "second.txt", "아폴로 보고서 B")

        after = engine.charters.load(PurePosixPath("아폴로/2023"))
        assert before is not None and after is not None
        assert after.purpose == before.purpose
        assert not _notes_for(llm, "아폴로/2023")

    async def test_deleting_a_document_preserves_the_folder_boundary_sign(
        self, engine: Bismuth, llm: FakeLLM, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023"))
        await add(engine, "a.txt", "아폴로 계약 A")
        await add(engine, "b.txt", "아폴로 보고서 B")
        before = engine.charters.load(PurePosixPath("아폴로/2023"))

        await engine.deletion.delete_file(PurePosixPath("아폴로/2023/b.txt"))

        after = engine.charters.load(PurePosixPath("아폴로/2023"))
        assert before is not None and after is not None
        assert after.purpose == before.purpose
        assert not _notes_for(llm, "아폴로/2023")


class TestStructureChanges:
    """Subdivision is the only thing that changes the shape of the tree now -- placement
    chooses among folders and never makes one -- so it is what has to leave the parent's
    note true (SPEC.md 3.6)."""

    async def _split_out_a_child(self, engine: Bismuth, script: ScriptedModel) -> None:
        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023"))
        await add(engine, "a.txt", "아폴로 계약 A")
        await add(engine, "b.txt", "아폴로 보고서 B")

        ids = [document_id for document_id, _ in engine.catalog.iter_cards()]
        script.set(
            subdivision_prompts.Emerging,
            subdivision_prompts.Emerging(
                emerged=True, axis="문서 종류", name="계약", note="계약서"
            ),
        )
        script.set(
            subdivision_prompts.Members,
            subdivision_prompts.Members(document_ids=ids[:1]),
        )
        await add(engine, "c.txt", "아폴로 계약 C")

    async def test_a_new_subfolder_gives_its_parent_a_note(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await self._split_out_a_child(engine, script)

        parent = engine.charters.load(PurePosixPath("아폴로/2023"))
        assert parent is not None
        assert parent.purpose

    async def test_structure_growth_does_not_turn_the_parent_sign_into_an_inventory(
        self, engine: Bismuth, script: ScriptedModel, llm: FakeLLM
    ) -> None:
        before = engine.charters.load(PurePosixPath("아폴로/2023"))
        await self._split_out_a_child(engine, script)

        after = engine.charters.load(PurePosixPath("아폴로/2023"))
        assert before is not None and after is not None
        assert after.purpose == before.purpose
        assert not _notes_for(llm, "아폴로/2023")

    async def test_an_audited_boundary_note_is_not_immediately_redrafted(
        self, engine: Bismuth, llm: FakeLLM
    ) -> None:
        class AlreadyNotedBoundary:
            async def consider_with_ancestors(
                self, *args: object, **kwargs: object
            ) -> list[Divided]:
                return [
                    Divided(
                        folder=PurePosixPath("아폴로/2023"),
                        created=(PurePosixPath("아폴로/2023/계약"),),
                        moved=1,
                    )
                ]

        engine.ingest._subdivision = AlreadyNotedBoundary()  # type: ignore[assignment]

        await add(engine, "one.txt", "아폴로 계약")

        assert not _notes_for(llm, "아폴로/2023")

    async def test_refresh_cannot_redefine_a_validated_boundary_sign(
        self, engine: Bismuth, script: ScriptedModel, llm: FakeLLM
    ) -> None:
        from tests.conftest import seed_folder

        root = Charter(
            path=PurePosixPath(),
            title="/",
            purpose="",
            split_basis="법적 효력 계층",
            split_question="이 문서의 법적 효력 계층은 무엇인가?",
            split_at_documents=10,
        )
        (engine.vault.root / "_folder.md").write_text(root.to_markdown(), encoding="utf-8")
        seed_folder(Path(engine.vault.root), PurePosixPath("대통령령"))
        sign = Charter(
            path=PurePosixPath("대통령령"),
            title="대통령령",
            purpose="법률의 위임에 따라 대통령이 제정하는 명령.",
        )
        (engine.vault.root / "대통령령/_folder.md").write_text(sign.to_markdown(), encoding="utf-8")
        script.set(placement_prompts.PlacementDecision, place_at("대통령령"))
        script.set(
            charter_prompts.CharterDraft,
            charter_prompts.CharterDraft(
                purpose="부처가 제정하는 시행규칙.",
            ),
        )

        await add(engine, "decree.txt", "대통령령 문서")

        refreshed = engine.charters.load(PurePosixPath("대통령령"))
        assert refreshed is not None
        assert refreshed.title == sign.title
        assert refreshed.purpose == sign.purpose
        assert not _notes_for(llm, "대통령령")


class TestHumanNotes:
    async def test_a_human_note_is_never_rewritten(
        self, engine: Bismuth, vault_path: Path, script: ScriptedModel
    ) -> None:
        # A note with managed: false is read but never redrawn.
        await add(engine, "first.txt", "아폴로 계약 A")

        human = Charter(
            path=PurePosixPath("아폴로/2023"),
            title="사람이 쓴 노트",
            purpose="이 폴더는 내가 직접 관리한다.",
            managed=False,
        ).to_markdown()
        note = vault_path / "아폴로/2023/_folder.md"
        note.write_text(human, encoding="utf-8")

        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023"))
        await add(engine, "second.txt", "아폴로 보고서 B")

        assert note.read_text(encoding="utf-8") == human
