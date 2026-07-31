"""Folder note (charter) refresh on document and subfolder changes."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from tests.conftest import ScriptedModel
from tests.test_ingest import add, place_at

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.container import Bismuth
from bismuth.domain.charter import Charter
from bismuth.prompts import charters as charter_prompts
from bismuth.prompts import placement as placement_prompts


def _notes_for(llm: FakeLLM, folder: str) -> list[str]:
    """The user-prompt text of every note draft aimed at ``folder``, in order."""
    return [
        p.user
        for p in llm.prompts_for(charter_prompts.CharterDraft)
        if p.user.startswith(f"FOLDER: {folder}\n")
    ]


class TestDocumentChanges:
    async def test_filing_into_an_existing_folder_redraws_its_note(
        self, engine: Bismuth, llm: FakeLLM, script: ScriptedModel
    ) -> None:
        await add(engine, "first.txt", "아폴로 계약 A")
        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023", existing=True))
        await add(engine, "second.txt", "아폴로 보고서 B")

        drafts = _notes_for(llm, "아폴로/2023")
        assert drafts, "the destination folder's note was never redrawn"
        assert "문서 2개" in drafts[-1]

    async def test_deleting_a_document_redraws_the_folder_note(
        self, engine: Bismuth, llm: FakeLLM, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023", existing=True))
        await add(engine, "a.txt", "아폴로 계약 A")
        await add(engine, "b.txt", "아폴로 보고서 B")

        await engine.deletion.delete_file(PurePosixPath("아폴로/2023/b.txt"))

        drafts = _notes_for(llm, "아폴로/2023")
        assert "문서 1개" in drafts[-1]


class TestStructureChanges:
    async def test_a_new_subfolder_gives_its_parent_a_note(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")

        parent = engine.charters.load(PurePosixPath("아폴로"))
        assert parent is not None
        assert parent.purpose

    async def test_the_parent_note_names_its_subfolders(
        self, engine: Bismuth, llm: FakeLLM
    ) -> None:
        await add(engine, "contract.txt")

        drafts = _notes_for(llm, "아폴로")
        assert drafts, "the parent folder's note was never drawn"
        assert "2023" in drafts[-1]


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

        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023", existing=True))
        await add(engine, "second.txt", "아폴로 보고서 B")

        assert note.read_text(encoding="utf-8") == human
