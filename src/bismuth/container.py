"""The composition root: the one place that knows both halves of the program."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentkit import ChatModel

from bismuth.adapters.catalog import FileCatalog
from bismuth.adapters.journal import JOURNAL_FILENAME, JsonlJournal
from bismuth.adapters.ledger import LEDGER_FILENAME, JsonlSpendLedger
from bismuth.adapters.llm import LiteLLMAdapter
from bismuth.adapters.llm.chat import LiteLLMChatModel
from bismuth.adapters.parsers import build_registry
from bismuth.adapters.vault import FileSystemVault
from bismuth.config import Settings
from bismuth.ports.catalog import Catalog
from bismuth.ports.journal import JournalStore
from bismuth.ports.ledger import SpendLedger
from bismuth.ports.llm import LLM
from bismuth.ports.parser import ParserRegistry
from bismuth.ports.vault import STATE_DIR, Vault
from bismuth.services.agent import AgentService
from bismuth.services.cards import CardService
from bismuth.services.charters import CharterService
from bismuth.services.deletion import DeletionService
from bismuth.services.ingest import IngestService
from bismuth.services.move import MoveService
from bismuth.services.placement import PlacementService
from bismuth.services.subdivision import LibraryMaintenanceService
from bismuth.services.transactor import Transactor


@dataclass(frozen=True, slots=True)
class Bismuth:
    """A wired-up engine over one vault."""

    settings: Settings
    llm: LLM
    chat: ChatModel
    """The agent's model. Exposed so its calls can be counted too -- a spend total that
    omits the post-upload review is wrong, not just incomplete."""
    vault: Vault
    catalog: Catalog
    journal: JournalStore
    ledger: SpendLedger
    """What this vault has cost so far. Survives the tab that spent it."""
    parsers: ParserRegistry
    transactor: Transactor
    cards: CardService
    charters: CharterService
    placement: PlacementService
    maintenance: LibraryMaintenanceService
    """Legacy deterministic planner, retained for diagnostics but not wired to ingest."""
    subdivision: LibraryMaintenanceService
    """Compatibility name for the diagnostic deterministic planner."""
    ingest: IngestService
    deletion: DeletionService
    move: MoveService
    agent: AgentService
    """The autonomous librarian: tool navigation, shadow validation, atomic application."""

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
    ledger = JsonlSpendLedger(state / LEDGER_FILENAME)
    catalog = FileCatalog(state)
    parsers = build_registry()

    model: LLM = llm or LiteLLMAdapter(
        model=settings.model_for(),
        api_key=settings.api_key,
        api_base=settings.api_base,
        timeout=settings.llm_timeout_seconds,
        absolute_timeout=settings.llm_absolute_timeout_seconds,
        max_schema_retries=settings.llm_max_schema_retries,
        max_concurrency=settings.llm_max_concurrency,
        headers=settings.api_headers,
        body=settings.api_body,
        native_schema=settings.native_schema,
    )
    chat: ChatModel = chat_model or LiteLLMChatModel(
        model=settings.model_for(),
        api_key=settings.api_key,
        api_base=settings.api_base,
        timeout=settings.llm_timeout_seconds,
        absolute_timeout=settings.llm_absolute_timeout_seconds,
        max_concurrency=settings.llm_max_concurrency,
        headers=settings.api_headers,
        body=settings.api_body,
    )

    transactor = Transactor(vault, journal)
    cards = CardService(
        model,
        context_chars=settings.card_context_chars,
        max_windows=settings.card_max_windows,
    )
    charters = CharterService(vault, model, catalog)
    placement = PlacementService(model)
    move = MoveService(vault=vault, transactor=transactor, charters=charters)
    maintenance = LibraryMaintenanceService(
        vault=vault, catalog=catalog, charters=charters, transactor=transactor, llm=model
    )

    return Bismuth(
        settings=settings,
        llm=model,
        chat=chat,
        vault=vault,
        catalog=catalog,
        journal=journal,
        ledger=ledger,
        parsers=parsers,
        transactor=transactor,
        cards=cards,
        charters=charters,
        placement=placement,
        maintenance=maintenance,
        subdivision=maintenance,
        ingest=IngestService(
            vault=vault,
            catalog=catalog,
            parsers=parsers,
            cards=cards,
            placement=placement,
            charters=charters,
            transactor=transactor,
            # Semantic maintenance now runs once over a completed upload batch.  The
            # old per-document deterministic planner remains available for diagnostics,
            # but must not reshape the live tree after every arrival.
            subdivision=None,
            extraction_max_chars=settings.extraction_max_chars,
        ),
        deletion=DeletionService(
            vault=vault, catalog=catalog, transactor=transactor, charters=charters
        ),
        move=move,
        agent=AgentService(
            model=chat,
            vault=vault,
            charters=charters,
            transactor=transactor,
        ),
    )
