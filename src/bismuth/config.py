"""Configuration. Bismuth owns it; it does not go looking for it."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from bismuth.ports.llm import ModelProfile

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
        label="OpenAI 호환 엔드포인트 (vLLM, Ollama, LM Studio, 사내 프록시…)",
        litellm_prefix="openai",
        needs_key=False,
        needs_api_base=True,
        default_api_base="http://localhost:11434/v1",
        hint=(
            "OpenAI 프로토콜(/chat/completions)을 쓰는 것이면 무엇이든 됩니다. "
            "모델 목록을 못 받아오면 모델 이름을 직접 적으면 됩니다."
        ),
    ),
)


def provider(provider_id: str) -> Provider | None:
    return next((p for p in PROVIDERS if p.id == provider_id), None)


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
    model_fast: str = Field(default="", description="High-volume work. Bare model name.")
    model_reasoning: str = Field(
        default="", description="Decisions worth paying for. Bare model name."
    )

    reasoning_effort: str = Field(
        default="",
        description=(
            "How hard the REASONING profile thinks, when the provider takes it "
            "(OpenAI: minimal/low/medium/high). Empty leaves the provider's default. "
            "Applies to that profile only, so it can be varied without also changing "
            "cataloguing."
        ),
    )

    llm_timeout_seconds: float = 120.0
    llm_max_schema_retries: int = Field(default=2, ge=0)
    llm_max_concurrency: int = Field(default=4, ge=1)
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

    @property
    def provider(self) -> Provider | None:
        return provider(self.provider_id)

    @property
    def is_configured(self) -> bool:
        """Whether Bismuth can call a model. Gates the setup wizard, nothing else."""
        chosen = self.provider
        if chosen is None or not self.model_fast or not self.model_reasoning:
            return False
        if chosen.needs_key and not self.api_key:
            return False
        return not (chosen.needs_api_base and not self.api_base)

    def model_for(self, profile: ModelProfile) -> str:
        """Resolve a profile to the string LiteLLM wants. The only place this mapping exists."""
        bare = self.model_fast if profile is ModelProfile.FAST else self.model_reasoning
        chosen = self.provider
        if chosen is None or "/" in bare:
            return bare
        return f"{chosen.litellm_prefix}/{bare}"

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
        data["api_headers"] = {name: _tail(value) for name, value in self.api_headers.items()}
        return data


def _tail(value: str) -> str:
    """Enough to tell two credentials apart, not enough to use one."""
    return f"…{value[-4:]}" if len(value) > 4 else "…"


def save_user_config(settings: Settings) -> Path:
    """Persist the wizard's answers to :data:`CONFIG_FILE`."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "vault_path": str(settings.vault_path),
        "provider_id": settings.provider_id,
        "api_key": settings.api_key,
        "api_base": settings.api_base,
        "api_headers": settings.api_headers,
        "model_fast": settings.model_fast,
        "model_reasoning": settings.model_reasoning,
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
