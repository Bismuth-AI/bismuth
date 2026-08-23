"""Configuration. Bismuth owns it; it does not go looking for it."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

CONFIG_DIR = Path.home() / ".bismuth"
CONFIG_FILE = CONFIG_DIR / "config.json"


class Provider(BaseModel):
    """A model backend Bismuth knows how to set up."""

    model_config = {"frozen": True}

    id: str
    label: str
    key_label: str = Field(default="API 키", description="What to call the credential in the UI.")
    needs_key: bool = True
    needs_api_base: bool = False
    default_api_base: str | None = None
    litellm_prefix: str = Field(description="What LiteLLM wants in front of the model name.")
    hint: str = ""


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="anthropic",
        label="Anthropic",
        litellm_prefix="anthropic",
        hint="console.anthropic.com → API keys 에서 발급",
    ),
    Provider(
        id="openai",
        label="OpenAI",
        litellm_prefix="openai",
        hint="platform.openai.com → API keys 에서 발급",
    ),
    Provider(
        id="custom",
        label="OpenAI 호환 서버",
        litellm_prefix="openai",
        needs_key=False,
        needs_api_base=True,
        default_api_base="http://localhost:11434/v1",
        hint="vLLM · Ollama · LM Studio · 사내 프록시 등",
    ),
)


def provider(provider_id: str) -> Provider | None:
    return next((p for p in PROVIDERS if p.id == provider_id), None)


ApiMode = Literal["auto", "responses", "chat_completions"]
ReasoningEffort = Literal["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One model behind one address, including how that model should be called."""

    provider_id: str
    model: str
    api_key: str
    api_base: str | None
    headers: dict[str, str]
    body: dict[str, Any]
    api_mode: ApiMode = "auto"
    reasoning_effort: ReasoningEffort = "auto"

    def for_workload(self, *, uses_tools: bool) -> Endpoint:
        """Resolve automatic model settings for one kind of work.

        A model name is not enough to choose an OpenAI wire protocol. GPT-5.6 tool
        calls are a Responses workload; leaving the effort absent keeps LiteLLM on
        Chat Completions and OpenAI refuses that combination. Custom OpenAI-compatible
        servers are deliberately excluded from the automatic rule because many do not
        implement Responses at all; their operator can opt in explicitly.
        """
        body = dict(self.body)
        openai_gpt56_tools = (
            uses_tools
            and self.provider_id == "openai"
            and _bare_model(self.model).startswith("gpt-5.6")
        )
        responses = self.api_mode == "responses" or (self.api_mode == "auto" and openai_gpt56_tools)
        model = (
            _with_responses_mode(self.model) if responses else _without_responses_mode(self.model)
        )

        if self.api_mode == "chat_completions" and openai_gpt56_tools:
            # This is the one reasoning value OpenAI permits beside function tools on
            # Chat Completions. The transport contract owns it over every other setting.
            body["reasoning_effort"] = "none"
        elif self.reasoning_effort != "auto":
            body["reasoning_effort"] = self.reasoning_effort
        elif responses and openai_gpt56_tools:
            body.setdefault("reasoning_effort", "low")

        return Endpoint(
            provider_id=self.provider_id,
            model=model,
            api_key=self.api_key,
            api_base=self.api_base,
            headers=dict(self.headers),
            body=body,
            api_mode=self.api_mode,
            reasoning_effort=self.reasoning_effort,
        )


def _bare_model(model: str) -> str:
    bare = model.split("/", 1)[1] if "/" in model else model
    return bare.removeprefix("responses/")


def _with_responses_mode(model: str) -> str:
    if "/" not in model:
        return f"responses/{_bare_model(model)}"
    prefix = model.split("/", 1)[0]
    return f"{prefix}/responses/{_bare_model(model)}"


def _without_responses_mode(model: str) -> str:
    if "/responses/" in model:
        return model.replace("/responses/", "/", 1)
    return model.removeprefix("responses/")


class Settings(BaseSettings):
    """Everything Bismuth needs to run."""

    model_config = SettingsConfigDict(
        env_prefix="BISMUTH_",
        env_file=".env",
        env_file_encoding="utf-8",
        json_file=CONFIG_FILE,
        json_file_encoding="utf-8",
        extra="ignore",
    )

    vault_path: Path = Field(
        default=Path.home() / "bismuth-vault",
        description="The directory Bismuth organises. The source of truth.",
    )
    provider_id: str = Field(default="", description="One of PROVIDERS. Empty until set up.")
    api_key: str = Field(
        default="",
        description=(
            "The credential, and the only one Bismuth reads. Provider variables in "
            "the ambient environment are deliberately ignored -- see the module "
            "docstring for the bug that taught us why."
        ),
    )
    api_base: str | None = Field(
        default=None, description="Endpoint override. Local backends need it."
    )
    api_headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Headers sent with every model call, on top of the credential. Some "
            "endpoints sit behind a gateway that authenticates with a cookie or its "
            "own header rather than a bearer token, and without this there is no way "
            "to reach them at all."
        ),
    )
    api_body: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Request-body values sent with every model call, applied over Bismuth's "
            "own. Sampling knobs a server wants (top_p, top_k, min_p) and switches only "
            "it knows about -- qwen's chat_template_kwargs.enable_thinking is the one "
            "that made this necessary: left on, a document took 93 seconds instead of 6."
        ),
    )
    native_schema: bool | None = Field(
        default=None,
        description=(
            "Whether the endpoint constrains decoding to a JSON Schema. None asks "
            "LiteLLM, which answers from a table of models it knows -- so anything "
            "self-hosted is 'no', and every structured call falls back to describing "
            "the schema in the prompt. Detected against the endpoint at setup instead."
        ),
    )
    model: str = Field(
        default="",
        description="The model that reads documents and shapes the tree.",
    )
    api_mode: ApiMode = Field(
        default="auto",
        description="How to call the filing model: automatic, Responses, or Chat Completions.",
    )
    reasoning_effort: ReasoningEffort = Field(
        default="auto", description="Reasoning effort for the filing model."
    )

    # -- the librarian at the desk, which need not be the one filing in the back.
    #
    # Filing reads one document at a time and answers in a fixed schema; answering a
    # question walks the tree over dozens of turns and writes prose. They are different
    # jobs and they were on one model only because there was one setting. Each of these
    # falls back to the value above when it is empty, so a vault that wants one model
    # for both keeps working without setting anything.
    chat_provider_id: str = Field(
        default="",
        description=(
            "The answering side's provider. Empty means the same one as above -- and "
            "then every field below falls back to its counterpart. Set, it makes the "
            "answering side a configuration in its own right: another company's model "
            "may answer questions about a vault a local one filed."
        ),
    )
    chat_model: str = Field(
        default="",
        description="The model that answers in 서고에 묻기. Empty means the one above.",
    )
    chat_api_key: str = Field(default="", description="Empty means the credential above.")
    chat_api_base: str | None = Field(default=None, description="None means the endpoint above.")
    chat_api_headers: dict[str, str] = Field(
        default_factory=dict, description="Empty means the headers above."
    )
    chat_api_body: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Empty means the body above. Set it when the two models want different "
            "sampling -- filing is decided at temperature 0, answering is not."
        ),
    )
    chat_api_mode: ApiMode = Field(
        default="auto",
        description="How to call the model used by 서고에 묻기.",
    )
    chat_reasoning_effort: ReasoningEffort = Field(
        default="auto", description="Reasoning effort for the model used by 서고에 묻기."
    )

    llm_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="Maximum silence after the last received LLM stream chunk.",
    )
    llm_absolute_timeout_seconds: float = Field(
        default=180.0,
        gt=0,
        description=(
            "Maximum provider execution time even while chunks keep arriving. Queue "
            "wait is excluded; per-schema output caps normally stop generation first."
        ),
    )
    llm_max_schema_retries: int = Field(default=2, ge=0)
    llm_max_concurrency: int = Field(
        default=12,
        ge=1,
        description=(
            "Model calls in flight at once. Measured on 300-document runs: 12 is safe and "
            "20 dropped documents. Was 4, which left the gateway idle most of the time."
        ),
    )
    ingest_read_ahead: int = Field(
        default=8,
        ge=1,
        description=(
            "How many documents are read and carded ahead of filing during a batch. "
            "Reading depends on the document alone; filing depends on the tree the "
            "documents before it built, so only the first half runs ahead."
        ),
    )
    extraction_max_chars: int = Field(default=200_000, ge=1_000)
    card_context_chars: int = Field(
        default=12_000,
        ge=500,
        description="Window size for cataloguing. The document is read window by window, not truncated to this.",
    )
    card_max_windows: int = Field(
        default=16,
        ge=1,
        description=(
            "Model calls one document may cost. A document with more windows than "
            "this is sampled across its whole length rather than read from the top, "
            "and the gap is recorded on the card."
        ),
    )
    chat_context_tokens: int = Field(
        default=64_000,
        ge=4_000,
        description=(
            "The chat model's context window. Not a cap on how hard a question may be "
            "worked: it is what the agent measures its transcript against, compacting "
            "old tool results when it nears the ceiling. Set too low it wastes the "
            "window; set too high the provider refuses a request, and the agent then "
            "adopts the limit the refusal states and carries on."
        ),
    )
    chat_budget_tokens: int = Field(
        default=400_000,
        ge=10_000,
        description=(
            "What one question may spend before the agent must answer from what it "
            "has. Deliberately separate from the window: how much can be held at once "
            "and how long the search may run are different questions, and tying them "
            "together doubles the bill for a change meant only to widen the desk."
        ),
    )
    host: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Precedence, highest first: argument > BISMUTH_* env var > ./.env > ~/.bismuth/config.json > default."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            JsonConfigSettingsSource(settings_cls),
        )

    @field_validator("vault_path")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_model_pair(cls, value: Any) -> Any:
        """Read old two-model config files once; the next save writes one model.

        The former judgement model is preferred because it was the capability ceiling
        users selected for placement and maintenance. Explicit new ``model`` always wins.
        """
        if not isinstance(value, dict) or value.get("model"):
            return value
        migrated = dict(value)
        migrated["model"] = value.get("model_reasoning") or value.get("model_fast") or ""
        return migrated

    @property
    def provider(self) -> Provider | None:
        return provider(self.provider_id)

    @property
    def is_configured(self) -> bool:
        """Whether Bismuth can call a model. Gates the setup wizard, nothing else."""
        chosen = self.provider
        if chosen is None or not self.model:
            return False
        if chosen.needs_key and not self.api_key:
            return False
        return not (chosen.needs_api_base and not self.api_base)

    def model_for(self) -> str:
        """Resolve the selected bare model name to the string LiteLLM wants."""
        return self._qualify(self.model)

    def _qualify(self, bare: str) -> str:
        chosen = self.provider
        if chosen is None or "/" in bare:
            return bare
        return f"{chosen.litellm_prefix}/{bare}"

    def librarian(self) -> Endpoint:
        """Where document reading and tree shaping are sent."""
        return Endpoint(
            provider_id=self.provider_id,
            model=self.model_for(),
            api_key=self.api_key,
            api_base=self.api_base,
            headers=self.api_headers,
            body=self.api_body,
            api_mode=self.api_mode,
            reasoning_effort=self.reasoning_effort,
        )

    def chat(self) -> Endpoint:
        """Where a question in 서고에 묻기 is sent.

        Two shapes, and which one applies is decided by ``chat_provider_id``:

        *Same provider* (it is empty). Every field falls back to its counterpart on its
        own, so naming a second model keeps the server, the credential and the switches
        that server needs. The body merges rather than replaces -- it carries switches,
        not preferences, and a chat body naming only ``temperature`` would have silently
        turned reasoning back on. A ``chat_api_base`` pointing at another host is still
        another host, so the key and headers stop there too: a credential belongs to the
        host it was issued for.

        *Its own provider* (it is set). Nothing is inherited. A key, a header or a
        sampling switch that belongs to one company's endpoint is meaningless at another
        and dangerous to send, so the answering side stands alone or not at all.
        """
        own = provider(self.chat_provider_id) if self.chat_provider_id else None
        if own is not None:
            bare = self.chat_model or self.model
            qualified = bare if "/" in bare else f"{own.litellm_prefix}/{bare}"
            return Endpoint(
                provider_id=own.id,
                model=qualified,
                api_key=self.chat_api_key,
                api_base=self.chat_api_base or own.default_api_base,
                headers=dict(self.chat_api_headers),
                body=dict(self.chat_api_body),
                api_mode=self.chat_api_mode,
                reasoning_effort=self.chat_reasoning_effort,
            )
        elsewhere = bool(self.chat_api_base) and self.chat_api_base != self.api_base
        return Endpoint(
            provider_id=self.provider_id,
            model=self._qualify(self.chat_model) if self.chat_model else self.model_for(),
            api_key=self.chat_api_key or ("" if elsewhere else self.api_key),
            api_base=self.chat_api_base or self.api_base,
            headers=self.chat_api_headers or ({} if elsewhere else self.api_headers),
            body={**self.api_body, **self.chat_api_body},
            api_mode=self.chat_api_mode,
            reasoning_effort=self.chat_reasoning_effort,
        )

    @property
    def chat_is_separate(self) -> bool:
        """Whether answering is pointed anywhere other than filing."""
        return self.chat() != self.librarian()

    @property
    def runs_locally(self) -> bool:
        """Whether this configuration keeps every byte on the machine.

        Read off the address, never assumed from the provider's name: the same server is
        local on this machine and not local on someone else's.
        """
        if self.api_base:
            return any(
                host in self.api_base for host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
            )
        return False

    def redacted(self) -> dict[str, Any]:
        """Safe to log, safe to render, safe to paste into an issue."""
        data = self.model_dump(mode="json")
        if self.api_key:
            data["api_key"] = f"…{self.api_key[-4:]}"
        # Headers are here because a bearer token was not enough, which means whatever
        # is in them is a credential too. The first one written was a session cookie,
        # and it went into bismuth.log in full.
        if self.chat_api_key:
            data["chat_api_key"] = f"…{self.chat_api_key[-4:]}"
        data["api_headers"] = {name: _tail(value) for name, value in self.api_headers.items()}
        data["chat_api_headers"] = {
            name: _tail(value) for name, value in self.chat_api_headers.items()
        }
        return data


def _tail(value: str) -> str:
    """Enough to tell two credentials apart, not enough to use one."""
    return f"…{value[-4:]}" if len(value) > 4 else "…"


class UserConfig(BaseModel):
    """One answered setup wizard, and the whole of what :data:`CONFIG_FILE` holds.

    Deliberately not a :class:`Settings`. Settings resolves four sources and *merges*
    them, and a merge cannot say "this endpoint has no headers": pydantic-settings
    deep-merges dict-valued fields, so ``Settings(api_headers={})`` keeps whatever the
    config file had. Every scalar answer replaced the old one and the two dicts did not.

    Switching provider therefore carried the previous endpoint's configuration onto the
    new one. Measured: a private gateway's session Cookie and a qwen-only
    ``chat_template_kwargs`` stayed attached after the provider was changed to OpenAI.
    The second came back ``400 Unknown parameter``. The first was sent to a third party
    and did not come back at all.

    So the wizard writes from here and re-reads Settings afterwards. Persisting cannot
    inherit, and what a ``BISMUTH_*`` variable still outranks on the way back is the
    documented precedence.
    """

    vault_path: Path
    provider_id: str
    api_key: str = ""
    api_base: str | None = None
    api_headers: dict[str, str] = Field(default_factory=dict)
    api_body: dict[str, Any] = Field(default_factory=dict)
    native_schema: bool | None = None
    model: str = ""
    api_mode: ApiMode = "auto"
    reasoning_effort: ReasoningEffort = "auto"
    chat_provider_id: str = ""
    chat_model: str = ""
    chat_api_key: str = ""
    chat_api_base: str | None = None
    chat_api_headers: dict[str, str] = Field(default_factory=dict)
    chat_api_body: dict[str, Any] = Field(default_factory=dict)
    chat_api_mode: ApiMode = "auto"
    chat_reasoning_effort: ReasoningEffort = "auto"

    @field_validator("vault_path")
    @classmethod
    def _expand_vault_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @property
    def is_configured(self) -> bool:
        """The same question :meth:`Settings.is_configured` asks, before anything is written."""
        chosen = provider(self.provider_id)
        if chosen is None or not self.model:
            return False
        if chosen.needs_key and not self.api_key:
            return False
        return not (chosen.needs_api_base and not self.api_base)


def save_user_config(config: UserConfig) -> Path:
    """Persist the wizard's answers to :data:`CONFIG_FILE`, replacing what was there."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "vault_path": str(config.vault_path),
        "provider_id": config.provider_id,
        "api_key": config.api_key,
        "api_base": config.api_base,
        "api_headers": config.api_headers,
        "api_body": config.api_body,
        "native_schema": config.native_schema,
        "model": config.model,
        "api_mode": config.api_mode,
        "reasoning_effort": config.reasoning_effort,
        "chat_provider_id": config.chat_provider_id,
        "chat_model": config.chat_model,
        "chat_api_key": config.chat_api_key,
        "chat_api_base": config.chat_api_base,
        "chat_api_headers": config.chat_api_headers,
        "chat_api_body": config.chat_api_body,
        "chat_api_mode": config.chat_api_mode,
        "chat_reasoning_effort": config.chat_reasoning_effort,
    }

    # Temp file + chmod before write: no half-written config, key never world-readable.
    handle, temp_name = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config-", suffix=".json")
    try:
        os.chmod(temp_name, stat.S_IRUSR | stat.S_IWUSR)  # 0600; a no-op on Windows
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, ensure_ascii=False)
        os.replace(temp_name, CONFIG_FILE)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return CONFIG_FILE


def load_env_file(path: Path | None = None) -> None:
    """Load ``./.env`` into the environment, for people who prefer files."""
    load_dotenv(path or Path(".env"), override=False)
