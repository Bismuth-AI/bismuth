"""End-to-end ingest: real engine, filesystem, and journal; only the model is scripted."""

from __future__ import annotations

from pathlib import PurePosixPath

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.container import Bismuth
from bismuth.domain.placement import Verdict
from bismuth.prompts import placement as placement_prompts
from bismuth.services.sidecar import read_sidecar_meta
from tests.conftest import ScriptedModel


async def add(engine: Bismuth, name: str, body: str = "아폴로 지원 계약서, 2023.") -> object:
    rel = engine.ingest.stage(body.encode("utf-8"), name)
    return await engine.ingest.process(rel)


def place_at(folder: str | None, *, confidence: float = 0.9, existing: bool = False):
    return placement_prompts.PlacementDecision(
        folder=folder, existing=existing, confidence=confidence, reason="테스트"
    )


class TestPlacement:
    async def test_first_document_creates_the_first_folder(self, engine: Bismuth) -> None:
        result = await add(engine, "contract.txt")

        assert result.placement.verdict is Verdict.PLACED
        assert result.destination == PurePosixPath("아폴로/2023")
        assert result.placement.created_folder is True
        assert (engine.vault.root / "아폴로/2023/contract.txt").is_file()

    async def test_the_file_is_moved_not_copied_and_never_edited(self, engine: Bismuth) -> None:
        body = "아폴로 지원 계약서, 2023. 고유 내용."
        await add(engine, "contract.txt", body)

        assert not (engine.vault.root / "_inbox/contract.txt").exists()
        assert (engine.vault.root / "아폴로/2023/contract.txt").read_text(encoding="utf-8") == body

    async def test_a_new_folder_gets_a_note(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")

        charter = engine.charters.load(PurePosixPath("아폴로/2023"))
        assert charter is not None
        assert charter.purpose

    async def test_the_model_sees_the_existing_tree_when_placing(
        self, engine: Bismuth, llm: FakeLLM, script: ScriptedModel
    ) -> None:
        await add(engine, "first.txt", "아폴로 계약서 내용 A")
        await add(engine, "second.txt", "아폴로 보고서 내용 B")

        prompt = llm.prompts_for(placement_prompts.PlacementDecision)[-1]
        assert "아폴로/2023" in prompt.user

    async def test_a_second_document_reuses_an_existing_folder(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await add(engine, "first.txt", "아폴로 계약서 A")
        # The model now returns the existing folder rather than a new one.
        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023", existing=True))

        result = await add(engine, "second.txt", "아폴로 보고서 B")

        assert result.destination == PurePosixPath("아폴로/2023")
        assert result.placement.created_folder is False
        assert (engine.vault.root / "아폴로/2023/second.txt").is_file()

    async def test_a_deep_path_is_created_whole(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at("법무/계약/대한물산/2023"))

        result = await add(engine, "deep.txt")

        assert result.destination == PurePosixPath("법무/계약/대한물산/2023")
        assert (engine.vault.root / "법무/계약/대한물산/2023/deep.txt").is_file()

    async def test_a_hostile_path_cannot_escape_the_vault(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at("../../../etc/pwned"))

        await add(engine, "evil.txt")

        assert not (engine.vault.root.parent.parent / "etc" / "pwned").exists()

    async def test_identical_content_is_not_re_ingested(
        self, engine: Bismuth, llm: FakeLLM
    ) -> None:
        await add(engine, "contract.txt")
        calls = llm.call_count

        result = await add(engine, "contract (1).txt")

        assert result.duplicate is True
        assert llm.call_count == calls
        assert engine.catalog.card_count() == 1


class TestSidecar:
    async def test_sidecar_makes_the_document_greppable(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt", "계약 기간은 24개월로 한다.")
        text = (engine.vault.root / "아폴로/2023/contract.txt.md").read_text(encoding="utf-8")
        assert "계약 기간은 24개월로 한다." in text

    async def test_sidecar_header_gives_a_grep_hit_its_context(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        text = (engine.vault.root / "아폴로/2023/contract.txt.md").read_text(encoding="utf-8")

        assert "아폴로 지원 계약서" in text  # title
        assert "계약서" in text  # doc_type
        assert "아폴로 계약 기간은?" in text  # a routing question

    async def test_sidecar_carries_its_own_identity(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        meta = read_sidecar_meta(
            (engine.vault.root / "아폴로/2023/contract.txt.md").read_text(encoding="utf-8")
        )
        assert meta is not None
        assert meta["document_id"]
        assert meta["topics"] == ["아폴로", "유지보수", "2023"]


class TestRefusal:
    """Placement declines to the inbox: null folder, low confidence, unusable path."""

    async def test_the_model_declining_parks_the_document(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at(None))

        result = await add(engine, "garbage.txt")

        assert result.placement.verdict is Verdict.INBOX
        assert (engine.vault.root / "_inbox/garbage.txt").is_file()

    async def test_low_confidence_parks_the_document(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023", confidence=0.2))

        result = await add(engine, "vague.txt")

        assert result.placement.verdict is Verdict.INBOX
        assert (engine.vault.root / "_inbox/vague.txt").is_file()

    async def test_an_unusable_path_parks_the_document(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at("...///..."))

        result = await add(engine, "weird.txt")

        assert result.placement.verdict is Verdict.INBOX


class TestUndo:
    async def test_filing_is_undoable(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        assert (engine.vault.root / "아폴로/2023/contract.txt").is_file()

        entry = next(e for e in engine.journal.iter_entries() if "-> 아폴로/2023" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "_inbox/contract.txt").is_file()
        assert not (engine.vault.root / "아폴로/2023/contract.txt").exists()
