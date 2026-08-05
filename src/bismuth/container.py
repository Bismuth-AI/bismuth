"""The composition root: the one place that knows both halves of the program."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentkit import ChatModel

from bismuth.adapters.catalog import FileCatalog
from bismuth.adapters.journal import JOURNAL_FILENAME, JsonlJournal
from bismuth.adapters.llm import LiteLLMAdapter
from bismuth.adapters.llm.chat import LiteLLMChatModel
from bismuth.adapters.parsers import build_registry
from bismuth.adapters.vault import FileSystemVault
from bismuth.config import Settings
from bismuth.ports.catalog import Catalog
from bismuth.ports.journal import JournalStore
from bismuth.ports.llm import LLM, ModelProfile
from bismuth.ports.parser import ParserRegistry
from bismuth.ports.vault import STATE_DIR, Vault
from bismuth.services.agent import AgentService
from bismuth.services.cards import CardService
from bismuth.services.charters import CharterService
from bismuth.services.deletion import DeletionService
from bismuth.services.ingest import IngestService
from bismuth.services.move import MoveService
from bismuth.services.placement import PlacementService
from bismuth.services.transactor import Transactor


@dataclass(frozen=True, slots=True)
class Bismuth:
    """A wired-up engine over one vault."""

    settings: Settings
    llm: LLM
    vault: Vault
    catalog: Catalog
    journal: JournalStore
    parsers: ParserRegistry
    transactor: Transactor
    cards: CardService
    charters: CharterService
    placement: PlacementService
    ingest: IngestService
    deletion: DeletionService
    move: MoveService
    agent: AgentService

    def recover(self) -> int:
        """Roll back anything a crash left half-done. Returns the number of batches reversed."""
        return len(self.transactor.recover())


def build(
    settings: Settings, *, llm: LLM | None = None, chat_model: ChatModel | None = None
) -> Bismuth:
    """Wire an engine over ``settings.vault_path``. Pass a fake ``llm``/``chat_model`` to run offline."""
    vault = FileSystemVault(settings.vault_path)
    state = Path(vault.root) / STATE_DIR

    journal = JsonlJournal(state / JOURNAL_FILENAME)
    catalog = FileCatalog(state)
    parsers = build_registry()

    model: LLM = llm or LiteLLMAdapter(
        model_fast=settings.model_for(ModelProfile.FAST),
        model_reasoning=settings.model_for(ModelProfile.REASONING),
        api_key=settings.api_key,
        api_base=settings.api_base,
        timeout=settings.llm_timeout_seconds,
        max_schema_retries=settings.llm_max_schema_retries,
        max_concurrency=settings.llm_max_concurrency,
    )
    chat: ChatModel = chat_model or LiteLLMChatModel(
        model=settings.model_for(ModelProfile.REASONING),
        api_key=settings.api_key,
        api_base=settings.api_base,
        timeout=settings.llm_timeout_seconds,
        max_concurrency=settings.llm_max_concurrency,
    )

    transactor = Transactor(vault, journal)
    cards = CardService(
        model,
        context_chars=settings.card_context_chars,
        max_windows=settings.card_max_windows,
    )
    charters = CharterService(vault, model, catalog)
    placement = PlacementService(model, min_confidence=settings.placement_min_confidence)
    move = MoveService(vault=vault, transactor=transactor, charters=charters)

    return Bismuth(
        settings=settings,
        llm=model,
        vault=vault,
        catalog=catalog,
        journal=journal,
        parsers=parsers,
        transactor=transactor,
        cards=cards,
        charters=charters,
        placement=placement,
        ingest=IngestService(
            vault=vault,
            catalog=catalog,
            parsers=parsers,
            cards=cards,
            placement=placement,
            charters=charters,
            transactor=transactor,
            extraction_max_chars=settings.extraction_max_chars,
        ),
        deletion=DeletionService(
            vault=vault, catalog=catalog, transactor=transactor, charters=charters
        ),
        move=move,
        agent=AgentService(model=chat, vault=vault, charters=charters),
    )
