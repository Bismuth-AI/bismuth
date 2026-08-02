"""Deleting files and folders, and restoring them via undo."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from bismuth.container import Bismuth
from bismuth.domain.errors import VaultError
from tests.test_ingest import add


class TestDeleteFile:
    async def test_deletes_the_document_and_its_sidecar(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        doc = PurePosixPath("아폴로/2023/contract.txt")
        assert (engine.vault.root / doc).is_file()

        result = await engine.deletion.delete_file(doc)

        assert result.files == 1
        assert not (engine.vault.root / doc).exists()
        assert not (engine.vault.root / "아폴로/2023/contract.txt.md").exists()  # sidecar too

    async def test_forgets_the_card(self, engine: Bismuth) -> None:
        result = await add(engine, "contract.txt")
        assert engine.catalog.card_count() == 1

        await engine.deletion.delete_file(PurePosixPath("아폴로/2023/contract.txt"))

        assert engine.catalog.card_count() == 0
        assert engine.catalog.load_card(result.document_id) is None

    async def test_delete_is_undoable_and_restores_the_bytes(self, engine: Bismuth) -> None:
        body = "아폴로 계약서. 고유한 내용 12345."
        await add(engine, "contract.txt", body)
        doc = PurePosixPath("아폴로/2023/contract.txt")

        await engine.deletion.delete_file(doc)
        entry = next(e for e in engine.journal.iter_entries() if e.reason.startswith("delete"))
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / doc).read_text(encoding="utf-8") == body
        assert (engine.vault.root / "아폴로/2023/contract.txt.md").is_file()

    async def test_deleting_a_missing_file_is_a_clean_error(self, engine: Bismuth) -> None:
        with pytest.raises(VaultError, match="삭제할 파일이 없습니다"):
            await engine.deletion.delete_file(PurePosixPath("아폴로/2023/nope.txt"))

    async def test_delete_files_removes_several_in_one_batch(self, engine: Bismuth) -> None:
        await add(engine, "a.txt", "아폴로 계약 A")
        await add(engine, "b.txt", "아폴로 보고서 B")

        result = await engine.deletion.delete_files(
            [PurePosixPath("아폴로/2023/a.txt"), PurePosixPath("아폴로/2023/b.txt")]
        )

        assert result.files == 2
        assert engine.catalog.card_count() == 0
        # A single journal entry, so it undoes in one step.
        batch = [e for e in engine.journal.iter_entries() if e.reason == "delete 2 document(s)"]
        assert len(batch) == 1

    async def test_delete_files_refuses_if_any_is_missing(self, engine: Bismuth) -> None:
        await add(engine, "a.txt", "아폴로 계약 A")
        with pytest.raises(VaultError, match="삭제할 파일이 없습니다"):
            await engine.deletion.delete_files(
                [PurePosixPath("아폴로/2023/a.txt"), PurePosixPath("아폴로/2023/nope.txt")]
            )
        assert (engine.vault.root / "아폴로/2023/a.txt").is_file()  # nothing deleted

    async def test_deleting_a_folder_path_as_a_file_is_refused(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        with pytest.raises(VaultError):
            await engine.deletion.delete_file(PurePosixPath("아폴로/2023"))


class TestDeleteFolder:
    async def test_deletes_everything_under_it(self, engine: Bismuth) -> None:
        await add(engine, "a.txt", "아폴로 계약서 A")
        await add(engine, "b.txt", "아폴로 보고서 B")
        assert engine.vault.count_files(PurePosixPath("아폴로/2023")) == 2

        result = await engine.deletion.delete_folder(PurePosixPath("아폴로"))

        assert result.files == 2
        assert not (engine.vault.root / "아폴로").exists()
        assert engine.catalog.card_count() == 0

    async def test_folder_delete_is_one_undoable_batch(self, engine: Bismuth) -> None:
        await add(engine, "a.txt", "아폴로 계약서 A")
        await add(engine, "b.txt", "아폴로 보고서 B")

        await engine.deletion.delete_folder(PurePosixPath("아폴로"))
        entry = next(e for e in engine.journal.iter_entries() if "delete folder" in e.reason)
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "아폴로/2023/a.txt").is_file()
        assert (engine.vault.root / "아폴로/2023/b.txt").is_file()
        assert engine.charters.load(PurePosixPath("아폴로/2023")) is not None

    async def test_the_inbox_cannot_be_deleted(self, engine: Bismuth) -> None:
        with pytest.raises(VaultError, match="인박스는 삭제할 수 없습니다"):
            await engine.deletion.delete_folder(PurePosixPath("_inbox"))

    async def test_the_root_cannot_be_deleted(self, engine: Bismuth) -> None:
        with pytest.raises(VaultError, match="루트는 삭제할 수 없습니다"):
            await engine.deletion.delete_folder(PurePosixPath())

    async def test_deleting_a_missing_folder_is_a_clean_error(self, engine: Bismuth) -> None:
        with pytest.raises(VaultError, match="삭제할 폴더가 없습니다"):
            await engine.deletion.delete_folder(PurePosixPath("nope"))
