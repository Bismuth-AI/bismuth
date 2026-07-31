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
    local: bool = False
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
        id="ollama",
        label="Ollama — 내 컴퓨터에서 실행, 키 불필요, 아무것도 밖으로 안 나감",
        litellm_prefix="ollama",
        needs_key=False,
        needs_api_base=True,
        default_api_base="http://localhost:11434",
        local=True,
        hint="ollama.com 에서 설치한 뒤: ollama pull qwen3:8b",
    ),
    Provider(
        id="custom",
        label="OpenAI 호환 엔드포인트 (vLLM, LM Studio, 프록시…)",
        litellm_prefix="openai",
        needs_key=False,
        needs_api_base=True,
        default_api_base="http://localhost:8000/v1",
        hint="OpenAI 프로토콜을 쓰는 것이면 무엇이든 됩니다.",
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
    model_fast: str = Field(default="", description="High-volume work. Bare model name.")
    model_reasoning: str = Field(
        default="", description="Decisions worth paying for. Bare model name."
    )

    llm_timeout_seconds: float = 120.0
    llm_max_schema_retries: int = Field(default=2, ge=0)
    llm_max_concurrency: int = Field(default=4, ge=1)
    extraction_max_chars: int = Field(default=200_000, ge=1_000)
    card_context_chars: int = Field(default=12_000, ge=500)
    placement_min_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
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
        """Whether this configuration keeps every byte on the machine."""
        chosen = self.provider
        if chosen is not None and chosen.local:
            return True
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
        return data


def save_user_config(settings: Settings) -> Path:
    """Persist the wizard's answers to :data:`CONFIG_FILE`."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "vault_path": str(settings.vault_path),
        "provider_id": settings.provider_id,
        "api_key": settings.api_key,
        "api_base": settings.api_base,
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
