"""What every operator needs from the folder it is working on.

The four operators of ADR-0018 are written as mixins on one service, because each of them
needs the same collaborators and the same memories of what has already been refused here.
This is the contract between them: an operator may read anything declared below, and
:class:`~bismuth.services.subdivision.service.LibraryMaintenanceService` is what actually
provides it.

Declared rather than implied. A mixin that simply reaches for ``self._vault`` type-checks
nowhere and documents nothing; the same mixin declaring what it depends on says, in one
place, exactly how much of the service an operator is entitled to touch.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import PurePosixPath

from bismuth.domain.charter import Charter
from bismuth.domain.journal import Operation
from bismuth.ports.catalog import Catalog
from bismuth.ports.llm import LLM
from bismuth.ports.vault import Vault
from bismuth.prompts import subdivision as prompts
from bismuth.services.charters import CharterService
from bismuth.services.subdivision.reading import _Contents
from bismuth.services.transactor import Transactor


class NeedsAFolder(ABC):
    """The collaborators and the memories an operator may use."""

    _vault: Vault
    _catalog: Catalog
    _charters: CharterService
    _transactor: Transactor
    _llm: LLM

    _barren: dict[tuple[str, str], int]
    """A name that was proposed here and shelved nothing, and how big the folder was."""
    _merged: dict[str, int]
    """A shelf built here by grouping, and the evidence it was built on."""
    _dissolved: dict[tuple[str, str], int]
    """A shelf dissolved here by splitting, so grouping does not rebuild it at once."""
    _declined: dict[tuple[str, str], set[str]]
    """Documents that answered STAY to a folder's signs, keyed by those signs."""
    _not_an_answer: dict[tuple[str, str], set[str]]
    """Names the check turned down here, keyed by the question they failed to answer."""

    # -- reading the tree, none of which costs a model call ---------------------------
    @abstractmethod
    def _read(self, folder: PurePosixPath, *, recursive: bool = False) -> _Contents: ...
    @abstractmethod
    def _count_documents(self, folder: PurePosixPath, *, recursive: bool) -> int: ...
    @abstractmethod
    def _subtree_depth(self, folder: PurePosixPath) -> int: ...
    @abstractmethod
    def _names_in_use(self, *, except_under: PurePosixPath | None = None) -> frozenset[str]: ...
    @abstractmethod
    def _axes_above(self, folder: PurePosixPath) -> list[str]: ...
    @abstractmethod
    def _has_protected_descendant(self, folder: PurePosixPath) -> bool: ...

    # -- what a decision writes down --------------------------------------------------
    @abstractmethod
    def _parent_note(
        self,
        folder: PurePosixPath,
        charter: Charter | None,
        plan: prompts.Division,
        *,
        documents: int,
    ) -> Charter: ...
    @abstractmethod
    def _stable_child_note_operations(
        self, folder: PurePosixPath, *, axis: str
    ) -> tuple[list[Operation], dict[PurePosixPath, bytes]]: ...
    @abstractmethod
    def _move_document(self, path: PurePosixPath, target: PurePosixPath) -> list[Operation]: ...
    @abstractmethod
    def _log_moves(self, folder: PurePosixPath, moves: list[tuple[str, PurePosixPath]]) -> None: ...

    # -- routing, which CREATE falls back to when nothing new has emerged -------------
    @abstractmethod
    async def _existing_assignments(
        self,
        *,
        folder: PurePosixPath,
        contents: _Contents,
        charter: Charter,
    ) -> prompts.ExistingAssignments: ...

    # -- the memory of a name that bought nothing -------------------------------------
    @abstractmethod
    def _asked_before(self, folder: PurePosixPath, name: str, *, documents: int) -> bool: ...
    @abstractmethod
    def _bought_nothing(self, folder: PurePosixPath, name: str, *, documents: int) -> None: ...
