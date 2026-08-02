"""Moving documents between folders, reversibly."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from bismuth.container import Bismuth
from bismuth.domain.errors import VaultError
from tests.test_ingest import add


class TestMove:
    async def test_moves_a_document_and_its_sidecar(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        src = PurePosixPath("아폴로/2023/contract.txt")

        result = await engine.move.move([src], "아폴로/2023/계약")

        assert result.moved == 1
        assert (engine.vault.root / "아폴로/2023/계약/contract.txt").is_file()
        assert (engine.vault.root / "아폴로/2023/계약/contract.txt.md").is_file()  # sidecar too
        assert not (engine.vault.root / "아폴로/2023/contract.txt").exists()

    async def test_the_target_folder_gets_a_note(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        await engine.move.move([PurePosixPath("아폴로/2023/contract.txt")], "아폴로/2023/계약")

        assert engine.charters.load(PurePosixPath("아폴로/2023/계약")) is not None

    async def test_move_is_undoable(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        await engine.move.move([PurePosixPath("아폴로/2023/contract.txt")], "아폴로/2023/계약")

        entry = next(e for e in engine.journal.iter_entries() if e.reason.startswith("move"))
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "아폴로/2023/contract.txt").is_file()
        assert not (engine.vault.root / "아폴로/2023/계약/contract.txt").exists()

    async def test_moving_into_the_inbox_is_refused(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        with pytest.raises(VaultError, match="인박스"):
            await engine.move.move([PurePosixPath("아폴로/2023/contract.txt")], "_inbox")

    async def test_moving_a_missing_file_is_a_clean_error(self, engine: Bismuth) -> None:
        with pytest.raises(VaultError, match="옮길 파일이 없습니다"):
            await engine.move.move([PurePosixPath("아폴로/2023/nope.txt")], "법무")

    async def test_move_to_the_same_folder_is_a_no_op(self, engine: Bismuth) -> None:
        await add(engine, "contract.txt")
        result = await engine.move.move([PurePosixPath("아폴로/2023/contract.txt")], "아폴로/2023")
        assert result.moved == 0
        assert (engine.vault.root / "아폴로/2023/contract.txt").is_file()


class TestRenameFolder:
    async def test_renames_the_whole_subtree(self, engine: Bismuth) -> None:
        await add(engine, "a.txt", "아폴로 계약 A")

        new_path = await engine.move.rename_folder(PurePosixPath("아폴로/2023"), "이천이십삼")

        assert new_path == "아폴로/이천이십삼"
        assert (engine.vault.root / "아폴로/이천이십삼/a.txt").is_file()
        assert not (engine.vault.root / "아폴로/2023").exists()

    async def test_rename_is_undoable(self, engine: Bismuth) -> None:
        await add(engine, "a.txt", "아폴로 계약 A")
        await engine.move.rename_folder(PurePosixPath("아폴로/2023"), "이천이십삼")

        entry = next(
            e for e in engine.journal.iter_entries() if e.reason.startswith("rename folder")
        )
        engine.transactor.undo(entry.id)

        assert (engine.vault.root / "아폴로/2023/a.txt").is_file()
        assert not (engine.vault.root / "아폴로/이천이십삼").exists()

    async def test_renaming_the_inbox_is_refused(self, engine: Bismuth) -> None:
        with pytest.raises(VaultError, match="인박스"):
            await engine.move.rename_folder(PurePosixPath("_inbox"), "x")

    async def test_renaming_a_missing_folder_is_refused(self, engine: Bismuth) -> None:
        with pytest.raises(VaultError, match="그런 폴더가 없습니다"):
            await engine.move.rename_folder(PurePosixPath("없는폴더"), "x")

    async def test_renaming_onto_an_existing_folder_is_refused(self, engine: Bismuth) -> None:
        await add(engine, "a.txt", "아폴로 계약 A")
        (engine.vault.root / "아폴로/기존").mkdir()
        with pytest.raises(VaultError, match="이미 있는 폴더"):
            await engine.move.rename_folder(PurePosixPath("아폴로/2023"), "기존")
