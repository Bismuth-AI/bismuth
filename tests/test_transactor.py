"""Journalling, rollback, undo, and crash recovery."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from bismuth.adapters.journal import JsonlJournal
from bismuth.adapters.vault import FileSystemVault
from bismuth.domain.errors import JournalCorruptError, VaultError
from bismuth.domain.journal import (
    Actor,
    EntryStatus,
    JournalEntry,
    Operation,
    OperationKind,
)
from bismuth.services.transactor import Transactor


@pytest.fixture
def vault(tmp_path: Path) -> FileSystemVault:
    return FileSystemVault(tmp_path / "vault")


@pytest.fixture
def journal(vault: FileSystemVault) -> JsonlJournal:
    return JsonlJournal(vault.root / ".bismuth" / "journal.jsonl")


@pytest.fixture
def transactor(vault: FileSystemVault, journal: JsonlJournal) -> Transactor:
    return Transactor(vault, journal)


def file_at(vault: FileSystemVault, rel: str, body: str = "hello") -> PurePosixPath:
    path = vault.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return PurePosixPath(rel)


def test_invalid_final_journal_record_is_not_treated_as_a_torn_write(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text('{"id":"not-a-complete-entry"}\n', encoding="utf-8")

    with pytest.raises(JournalCorruptError):
        list(JsonlJournal(path).iter_entries())


class TestOperationInversion:
    """Operation.inverse() logic; pure, no filesystem."""

    def test_move_inverts_to_the_opposite_move(self) -> None:
        move = Operation(
            kind=OperationKind.MOVE, source=PurePosixPath("a/x"), target=PurePosixPath("b/x")
        )
        inverse = move.inverse()
        assert inverse is not None
        assert inverse.kind is OperationKind.MOVE
        assert inverse.source == PurePosixPath("b/x")
        assert inverse.target == PurePosixPath("a/x")

    def test_write_over_nothing_inverts_to_remove(self) -> None:
        write = Operation(kind=OperationKind.WRITE, target=PurePosixPath("new.md"))
        assert write.inverse().kind is OperationKind.REMOVE  # type: ignore[union-attr]

    def test_write_over_something_inverts_to_restore(self) -> None:
        write = Operation(kind=OperationKind.WRITE, target=PurePosixPath("x.md"), backup_ref="abc")
        inverse = write.inverse()
        assert inverse is not None
        assert inverse.kind is OperationKind.RESTORE
        assert inverse.backup_ref == "abc"

    def test_inverses_run_in_reverse_order(self) -> None:
        # mkdir-then-move must invert to move-then-rmdir.
        entry = JournalEntry(
            reason="test",
            operations=(
                Operation(kind=OperationKind.MKDIR, target=PurePosixPath("new")),
                Operation(
                    kind=OperationKind.MOVE,
                    source=PurePosixPath("a"),
                    target=PurePosixPath("new/a"),
                ),
            ),
        )
        kinds = [op.kind for op in entry.inverse_operations()]
        assert kinds == [OperationKind.MOVE, OperationKind.RMDIR]


class TestExecution:
    def test_intent_is_journalled_before_anything_moves(
        self, vault: FileSystemVault, journal: JsonlJournal, transactor: Transactor
    ) -> None:
        source = file_at(vault, "_inbox/doc.txt")
        entry = transactor.execute(
            JournalEntry(
                reason="file doc.txt",
                operations=(
                    Operation(kind=OperationKind.MKDIR, target=PurePosixPath("Apollo")),
                    Operation(
                        kind=OperationKind.MOVE,
                        source=source,
                        target=PurePosixPath("Apollo/doc.txt"),
                    ),
                ),
            )
        )

        assert entry.status is EntryStatus.APPLIED
        assert (vault.root / "Apollo/doc.txt").is_file()
        assert journal.get(entry.id).status is EntryStatus.APPLIED  # type: ignore[union-attr]

    def test_a_failed_batch_leaves_no_trace(
        self, vault: FileSystemVault, journal: JsonlJournal, transactor: Transactor
    ) -> None:
        file_at(vault, "_inbox/a.txt")
        entry = JournalEntry(
            reason="doomed batch",
            operations=(
                Operation(kind=OperationKind.MKDIR, target=PurePosixPath("Apollo")),
                Operation(
                    kind=OperationKind.MOVE,
                    source=PurePosixPath("_inbox/a.txt"),
                    target=PurePosixPath("Apollo/a.txt"),
                ),
                Operation(
                    kind=OperationKind.MOVE,
                    source=PurePosixPath("_inbox/ghost.txt"),
                    target=PurePosixPath("Apollo/ghost.txt"),
                ),
            ),
        )

        with pytest.raises(VaultError):
            transactor.execute(entry)

        assert (vault.root / "_inbox/a.txt").is_file()
        assert not (vault.root / "Apollo/a.txt").exists()
        assert not (vault.root / "Apollo").exists()
        assert journal.get(entry.id).status is EntryStatus.FAILED  # type: ignore[union-attr]

    def test_refuses_to_silently_overwrite(
        self, vault: FileSystemVault, transactor: Transactor
    ) -> None:
        file_at(vault, "_inbox/doc.txt", "new")
        file_at(vault, "Apollo/doc.txt", "existing and precious")

        with pytest.raises(VaultError, match="refusing to overwrite"):
            transactor.execute(
                JournalEntry(
                    reason="collide",
                    operations=(
                        Operation(
                            kind=OperationKind.MOVE,
                            source=PurePosixPath("_inbox/doc.txt"),
                            target=PurePosixPath("Apollo/doc.txt"),
                        ),
                    ),
                )
            )
        assert (vault.root / "Apollo/doc.txt").read_text(
            encoding="utf-8"
        ) == "existing and precious"

    def test_paths_cannot_escape_the_vault(
        self, vault: FileSystemVault, transactor: Transactor
    ) -> None:
        with pytest.raises(VaultError, match="escapes the vault"):
            transactor.execute(
                JournalEntry(
                    reason="escape attempt",
                    operations=(
                        Operation(kind=OperationKind.MKDIR, target=PurePosixPath("../../pwned")),
                    ),
                )
            )


class TestUndo:
    def test_undo_puts_a_moved_file_back(
        self, vault: FileSystemVault, transactor: Transactor
    ) -> None:
        file_at(vault, "_inbox/doc.txt", "body")
        entry = transactor.execute(
            JournalEntry(
                reason="file it",
                operations=(
                    Operation(kind=OperationKind.MKDIR, target=PurePosixPath("Apollo")),
                    Operation(
                        kind=OperationKind.MOVE,
                        source=PurePosixPath("_inbox/doc.txt"),
                        target=PurePosixPath("Apollo/doc.txt"),
                    ),
                ),
            )
        )

        transactor.undo(entry.id)

        assert (vault.root / "_inbox/doc.txt").read_text(encoding="utf-8") == "body"
        assert not (vault.root / "Apollo/doc.txt").exists()

    def test_undo_restores_overwritten_content(
        self, vault: FileSystemVault, transactor: Transactor
    ) -> None:
        file_at(vault, "_folder.md", "the words a human wrote")
        entry = transactor.execute(
            JournalEntry(
                reason="rewrite charter",
                operations=(
                    Operation(kind=OperationKind.WRITE, target=PurePosixPath("_folder.md")),
                ),
            ),
            payloads={PurePosixPath("_folder.md"): b"what a model wrote"},
        )
        assert (vault.root / "_folder.md").read_text(encoding="utf-8") == "what a model wrote"

        transactor.undo(entry.id)

        assert (vault.root / "_folder.md").read_text(encoding="utf-8") == "the words a human wrote"

    def test_undo_is_itself_recorded_and_undoable(
        self, vault: FileSystemVault, journal: JsonlJournal, transactor: Transactor
    ) -> None:
        file_at(vault, "_inbox/doc.txt")
        original = transactor.execute(
            JournalEntry(
                reason="file it",
                operations=(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=PurePosixPath("_inbox/doc.txt"),
                        target=PurePosixPath("doc.txt"),
                    ),
                ),
            )
        )

        undo_entry = transactor.undo(original.id)

        assert journal.get(original.id).status is EntryStatus.REVERTED  # type: ignore[union-attr]
        assert undo_entry.actor is Actor.USER
        transactor.undo(undo_entry.id)  # redo, effectively
        assert (vault.root / "doc.txt").is_file()

    def test_cannot_undo_what_was_never_applied(self, transactor: Transactor) -> None:
        with pytest.raises(VaultError, match="no journal entry"):
            transactor.undo("nonexistent")


class TestCrashRecovery:
    def test_a_pending_entry_is_rolled_back_on_restart(
        self, vault: FileSystemVault, journal: JsonlJournal, transactor: Transactor
    ) -> None:
        """Simulates dying mid-batch: the intent is durable, the work is half-done."""
        file_at(vault, "_inbox/a.txt")
        file_at(vault, "_inbox/b.txt")

        entry = JournalEntry(
            reason="interrupted bulk move",
            operations=(
                Operation(kind=OperationKind.MKDIR, target=PurePosixPath("Apollo")),
                Operation(
                    kind=OperationKind.MOVE,
                    source=PurePosixPath("_inbox/a.txt"),
                    target=PurePosixPath("Apollo/a.txt"),
                ),
                Operation(
                    kind=OperationKind.MOVE,
                    source=PurePosixPath("_inbox/b.txt"),
                    target=PurePosixPath("Apollo/b.txt"),
                ),
            ),
        )
        # Only 2 of 3 operations applied before the simulated crash.
        journal.append(entry)
        vault.apply(entry.operations[0])
        vault.apply(entry.operations[1])

        recovered = transactor.recover()

        assert len(recovered) == 1
        assert recovered[0].status is EntryStatus.REVERTED
        assert (vault.root / "_inbox/a.txt").is_file()
        assert (vault.root / "_inbox/b.txt").is_file()
        assert not (vault.root / "Apollo").exists()
        assert journal.pending() == []

    def test_recovery_is_a_no_op_on_a_clean_vault(self, transactor: Transactor) -> None:
        assert transactor.recover() == []

    def test_rollback_never_deletes_a_folder_a_user_filled(
        self, vault: FileSystemVault, journal: JsonlJournal, transactor: Transactor
    ) -> None:
        entry = JournalEntry(
            reason="interrupted",
            operations=(Operation(kind=OperationKind.MKDIR, target=PurePosixPath("Apollo")),),
        )
        journal.append(entry)
        vault.apply(entry.operations[0])
        file_at(vault, "Apollo/precious.txt", "the user put this here")

        transactor.recover()

        assert (vault.root / "Apollo/precious.txt").read_text(
            encoding="utf-8"
        ) == "the user put this here"


def test_an_entry_whose_files_moved_again_says_so_instead_of_failing_halfway(
    vault: FileSystemVault, transactor: Transactor
) -> None:
    file_at(vault, "문서.txt")
    first = transactor.execute(
        JournalEntry(
            actor=Actor.BISMUTH,
            reason="divide",
            operations=(
                Operation(
                    kind=OperationKind.MOVE,
                    source=PurePosixPath("문서.txt"),
                    target=PurePosixPath("문학/문서.txt"),
                ),
            ),
        )
    )
    # A later pass files it somewhere else, exactly as subdivision does.
    transactor.execute(
        JournalEntry(
            actor=Actor.BISMUTH,
            reason="divide again",
            operations=(
                Operation(
                    kind=OperationKind.MOVE,
                    source=PurePosixPath("문학/문서.txt"),
                    target=PurePosixPath("문학/소설/문서.txt"),
                ),
            ),
        )
    )

    with pytest.raises(VaultError, match="moved again"):
        transactor.undo(first.id)

    # And nothing was disturbed by the refusal.
    assert (vault.root / "문학" / "소설" / "문서.txt").is_file()
    assert transactor._journal.get(first.id).status is EntryStatus.APPLIED
