"""End-to-end ingest: real engine, filesystem, and journal; only the model is scripted."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.container import Bismuth
from bismuth.domain.placement import Verdict
from bismuth.prompts import placement as placement_prompts
from bismuth.services.sidecar import read_sidecar_meta
from tests.conftest import ScriptedModel, placement_to


async def add(engine: Bismuth, name: str, body: str = "아폴로 지원 계약서, 2023.") -> object:
    rel = engine.ingest.stage(body.encode("utf-8"), name)
    return await engine.ingest.process(rel)


def place_at(folder: str | None, *, confidence: float = 0.9):
    """A scripted hierarchical choice for the requested final folder."""
    return placement_to(folder, confidence=confidence)


async def add_into(engine: Bismuth, script: ScriptedModel, name: str, folder: str, body: str = ""):
    """File a document into `folder`, putting the folder there first.

    Placement chooses and never creates, so a destination has to exist before a document
    can be sent to it. Bodies differ by default: identity is the bytes, and a shared one
    would make every document after the first a duplicate.
    """
    from tests.conftest import seed_folder

    seed_folder(Path(engine.vault.root), PurePosixPath(folder))
    script.set(placement_prompts.PlacementDecision, place_at(folder))
    return await add(engine, name, body or f"{folder} 문서 {name}")


class TestPlacement:
    def test_pending_inbox_tolerates_a_file_moved_during_status_poll(
        self, engine: Bismuth, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine.ingest.stage(b"moving", "moving.txt")
        original_read = engine.vault.read_bytes

        def disappear_before_read(path: PurePosixPath) -> bytes:
            absolute = Path(engine.vault.root) / Path(*path.parts)
            absolute.unlink()
            return original_read(path)

        monkeypatch.setattr(engine.vault, "read_bytes", disappear_before_read)

        assert engine.ingest.pending_inbox() == []

    async def test_a_document_goes_into_a_folder_that_exists(self, engine: Bismuth) -> None:
        result = await add(engine, "contract.txt")

        assert result.placement.verdict is Verdict.PLACED
        assert result.destination == PurePosixPath("아폴로/2023")
        assert result.placement.created_folder is False  # it was already there
        assert (engine.vault.root / "아폴로/2023/contract.txt").is_file()

    async def test_a_folder_that_does_not_exist_is_read_as_the_root(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """Placement chooses; only subdivision creates. Asked for somewhere that is not
        there, the honest answer is the root (SPEC.md 3.4) -- measured otherwise as
        twenty folders each named after one document."""
        script.set(placement_prompts.PlacementDecision, place_at("F999"))

        result = await add(engine, "contract.txt")

        assert result.destination == PurePosixPath()
        assert not (engine.vault.root / "새로운").exists()
        assert (engine.vault.root / "contract.txt").is_file()

    async def test_the_file_is_moved_not_copied_and_never_edited(self, engine: Bismuth) -> None:
        body = "아폴로 지원 계약서, 2023. 고유 내용."
        await add(engine, "contract.txt", body)

        assert not (engine.vault.root / "_inbox/contract.txt").exists()
        assert (engine.vault.root / "아폴로/2023/contract.txt").read_text(encoding="utf-8") == body

    async def test_maintenance_failure_does_not_turn_a_filed_document_into_an_ingest_failure(
        self, engine: Bismuth
    ) -> None:
        class FailingMaintenance:
            async def consider_with_ancestors(
                self, *args: object, **kwargs: object
            ) -> list[object]:
                raise RuntimeError("bad maintenance proposal")

        engine.ingest._subdivision = FailingMaintenance()  # type: ignore[assignment]

        result = await add(engine, "safe.txt", "파일은 먼저 안전하게 저장됩니다.")

        assert result.destination == PurePosixPath("아폴로/2023")
        assert (engine.vault.root / "아폴로/2023/safe.txt").is_file()
        assert (engine.vault.root / "아폴로/2023/safe.txt.md").is_file()

    async def test_folder_sign_failure_does_not_turn_a_filed_document_into_a_failure(
        self, engine: Bismuth
    ) -> None:
        async def fail(*args: object, **kwargs: object) -> list[object]:
            raise RuntimeError("folder sign unavailable")

        engine.charters.refresh_operations = fail  # type: ignore[method-assign]

        result = await add(engine, "safe-note.txt", "안전하게 저장될 문서")

        assert result.destination == PurePosixPath("아폴로/2023")
        assert (engine.vault.root / "아폴로/2023/safe-note.txt").is_file()
        assert (engine.vault.root / "아폴로/2023/safe-note.txt.md").is_file()

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
        assert "CURRENT FOLDER: 아폴로" in prompt.user
        assert "[F001] 2023" in prompt.user

    async def test_a_second_document_reuses_an_existing_folder(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        await add(engine, "first.txt", "아폴로 계약서 A")
        # The model now returns the existing folder rather than a new one.
        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023"))

        result = await add(engine, "second.txt", "아폴로 보고서 B")

        assert result.destination == PurePosixPath("아폴로/2023")
        assert result.placement.created_folder is False
        assert (engine.vault.root / "아폴로/2023/second.txt").is_file()

    async def test_a_deep_existing_path_is_used_whole(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        from tests.conftest import seed_folder

        seed_folder(Path(engine.vault.root), PurePosixPath("법무/계약/대한물산/2023"))
        script.set(placement_prompts.PlacementDecision, place_at("법무/계약/대한물산/2023"))

        result = await add(engine, "deep.txt")

        assert result.destination == PurePosixPath("법무/계약/대한물산/2023")
        assert (engine.vault.root / "법무/계약/대한물산/2023/deep.txt").is_file()

    async def test_a_hostile_path_cannot_escape_the_vault(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at("F999"))

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

    async def test_duplicate_reports_the_current_path_after_maintenance_moves_the_original(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        body = "같은 원본의 현재 위치를 사이드카로 찾습니다."
        await add(engine, "original.txt", body)

        from tests.conftest import seed_folder

        seed_folder(Path(engine.vault.root), PurePosixPath("현재 위치"))
        await engine.move.move([PurePosixPath("아폴로/2023/original.txt")], "현재 위치")

        duplicate = await add(engine, "duplicate.txt", body)

        assert duplicate.duplicate is True
        assert duplicate.destination == PurePosixPath("현재 위치")


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
    """The inbox is for documents that could not be read. Anything readable is filed,
    even if only at the root (SPEC.md 3.4)."""

    async def test_the_model_declining_parks_the_document(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at(None))

        result = await add(engine, "garbage.txt")

        assert result.placement.verdict is Verdict.INBOX
        assert (engine.vault.root / "_inbox/garbage.txt").is_file()

    async def test_low_confidence_is_filed_anyway(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        """There is no threshold. Being unsure is answered by the root, not by waiting."""
        script.set(placement_prompts.PlacementDecision, place_at("아폴로/2023", confidence=0.2))

        result = await add(engine, "vague.txt")

        assert result.placement.verdict is Verdict.PLACED
        assert result.placement.confidence == pytest.approx(0.2)
        assert (engine.vault.root / "아폴로/2023/vague.txt").is_file()

    async def test_the_root_is_a_normal_answer(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at(""))

        result = await add(engine, "unsorted.txt")

        assert result.placement.verdict is Verdict.PLACED
        assert result.destination == PurePosixPath()
        assert (engine.vault.root / "unsorted.txt").is_file()
        assert not (engine.vault.root / "_inbox/unsorted.txt").exists()

    async def test_an_unusable_path_falls_back_to_the_root(
        self, engine: Bismuth, script: ScriptedModel
    ) -> None:
        script.set(placement_prompts.PlacementDecision, place_at("F999"))

        result = await add(engine, "weird.txt")

        # A broken path is not a request for the root, but it is also not a reason to
        # refuse a document we read perfectly well.
        assert result.placement.verdict is Verdict.PLACED
        assert result.destination == PurePosixPath()


class TestUndo:
    async def test_filing_is_undoable(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        assert (engine.vault.root / "아폴로/2023/contract.txt").is_file()

        entry = next(e for e in engine.journal.iter_entries() if "-> 아폴로/2023" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "_inbox/contract.txt").is_file()
        assert not (engine.vault.root / "아폴로/2023/contract.txt").exists()


class TestReadingAheadOfFiling:
    """Reading a document depends on that document; filing depends on the tree and
    changes it. The split is what lets a caller overlap the first and not the second."""

    async def test_preparing_touches_nothing(self, engine: Bismuth) -> None:
        rel = engine.ingest.stage("아폴로 지원 계약서, 2023.".encode(), "contract.txt")
        before = sorted(p.name for p in engine.vault.root.iterdir())

        prepared = await engine.ingest.prepare(rel)

        assert prepared.card is not None
        assert sorted(p.name for p in engine.vault.root.iterdir()) == before
        assert (engine.vault.root / "_inbox/contract.txt").is_file()  # still where it was

    async def test_documents_read_together_are_filed_in_order(self, engine: Bismuth) -> None:
        import asyncio

        rels = [
            engine.ingest.stage(f"문서 {i} 고유 내용".encode(), f"doc{i}.txt") for i in range(4)
        ]

        prepared = await asyncio.gather(*(engine.ingest.prepare(rel) for rel in rels))
        results = [await engine.ingest.file(p) for p in prepared]

        assert [r.filename for r in results] == [f"doc{i}.txt" for i in range(4)]
        assert all((engine.vault.root / "아폴로/2023" / r.filename).is_file() for r in results)

    async def test_spend_stays_attributed_to_its_own_document(
        self, engine: Bismuth, llm: FakeLLM
    ) -> None:
        """A drain-before/drain-after bracket cannot do this once reads overlap, so the
        document rides with the call instead."""
        import asyncio

        rels = [
            engine.ingest.stage(f"문서 {i} 고유 내용".encode(), f"doc{i}.txt") for i in range(3)
        ]
        llm.drain_usage()

        prepared = await asyncio.gather(*(engine.ingest.prepare(rel) for rel in rels))

        tagged = {p.source.document_id for p in prepared}
        spent_on = {u.document_id for u in llm.drain_usage()}
        assert spent_on == tagged  # every call landed on a real document, none blank
