"""Settings precedence and credential handling: only BISMUTH_* is read, ambient provider env vars are ignored."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from bismuth.config import (
    PROVIDERS,
    Settings,
    UserConfig,
    load_env_file,
    provider,
    save_user_config,
)


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Neutralize all config sources: env vars, .env, and config.json."""
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("BISMUTH_") or key.endswith("_API_KEY"):
            del os.environ[key]

    monkeypatch.chdir(tmp_path)  # no ./.env here
    monkeypatch.setitem(Settings.model_config, "json_file", tmp_path / "no-config.json")
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Settings' JSON source at a file this test controls."""
    path = tmp_path / "config.json"
    monkeypatch.setitem(Settings.model_config, "json_file", path)
    return path


def configured(**overrides: object) -> Settings:
    base = {
        "provider_id": "openai",
        "api_key": "sk-test",
        "model": "gpt-4o",
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def answers(**overrides: object) -> UserConfig:
    """What the wizard would send for a working hosted provider."""
    base: dict[str, object] = {
        "vault_path": Path.home() / "bismuth-vault",
        "provider_id": "openai",
        "api_key": "sk-test",
        "model": "gpt-4o",
    }
    return UserConfig(**{**base, **overrides})  # type: ignore[arg-type]


class TestAmbientKeysAreIgnored:
    """The regression, stated as a rule: nothing ambient decides our credentials."""

    def test_a_provider_key_in_the_environment_is_not_used(self, clean_env: None) -> None:
        os.environ["OPENAI_API_KEY"] = "sk-the-dead-one-from-2019"
        assert Settings().api_key == ""

    def test_a_provider_key_in_a_dotenv_is_not_used(self, tmp_path: Path, clean_env: None) -> None:
        # load_dotenv still runs; only BISMUTH_API_KEY is read.
        env_file = tmp_path / ".env"
        env_file.write_text("OPENAI_API_KEY=sk-also-dead\n", encoding="utf-8")
        load_env_file(env_file)
        assert Settings().api_key == ""

    def test_the_prefixed_variable_is_the_one_that_works(self, clean_env: None) -> None:
        os.environ["BISMUTH_API_KEY"] = "sk-ours"
        assert Settings().api_key == "sk-ours"


class TestPrecedence:
    """argument > BISMUTH_* env > ./.env > ~/.bismuth/config.json > default."""

    def test_config_file_supplies_the_baseline(self, clean_env: None, config_file: Path) -> None:
        config_file.write_text(json.dumps({"api_key": "sk-from-wizard"}), encoding="utf-8")
        assert Settings().api_key == "sk-from-wizard"

    def test_an_env_var_beats_the_config_file(self, clean_env: None, config_file: Path) -> None:
        config_file.write_text(json.dumps({"api_key": "sk-from-wizard"}), encoding="utf-8")
        os.environ["BISMUTH_API_KEY"] = "sk-from-docker"
        assert Settings().api_key == "sk-from-docker"

    def test_a_dotenv_beats_the_config_file(self, clean_env: None, config_file: Path) -> None:
        config_file.write_text(json.dumps({"api_key": "sk-from-wizard"}), encoding="utf-8")
        Path(".env").write_text("BISMUTH_API_KEY=sk-from-dotenv\n", encoding="utf-8")
        assert Settings().api_key == "sk-from-dotenv"

    def test_an_argument_beats_everything(self, clean_env: None) -> None:
        os.environ["BISMUTH_API_KEY"] = "sk-from-env"
        assert Settings(api_key="sk-explicit").api_key == "sk-explicit"


class TestConfigFile:
    def test_saving_writes_only_what_a_human_chose(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Tuning knobs keep code defaults so upgrades can improve them.
        monkeypatch.setattr("bismuth.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("bismuth.config.CONFIG_FILE", tmp_path / "config.json")

        save_user_config(answers(vault_path=tmp_path / "v"))

        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["api_key"] == "sk-test"
        assert saved["model"] == "gpt-4o"
        assert "model_fast" not in saved
        assert "model_reasoning" not in saved
        assert "pressure_folder_size" not in saved
        assert "llm_timeout_seconds" not in saved
        assert "llm_absolute_timeout_seconds" not in saved

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_the_key_file_is_not_world_readable(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bismuth.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("bismuth.config.CONFIG_FILE", tmp_path / "config.json")

        path = save_user_config(answers())

        assert oct(path.stat().st_mode)[-3:] == "600"


class TestSwitchingProviderLeavesNothingBehind:
    """A private endpoint's configuration must not travel to a public one.

    Observed: a custom endpoint was set up with a gateway session Cookie and
    ``chat_template_kwargs`` for a qwen model, then the provider was changed to OpenAI in
    the wizard. Both survived the change. api.openai.com answered
    ``400 Unknown parameter: 'chat_template_kwargs'``, and the Cookie had already been
    sent to it by then.
    """

    def test_an_empty_dict_cannot_clear_one_the_config_file_holds(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Why the wizard cannot persist by constructing Settings, however explicit it is.

        pydantic-settings deep-merges dict-valued fields across sources, so the empty
        dict a higher-priority source passes contributes no keys instead of replacing
        them. Pinned because it reads like it should work.
        """
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {
                    "provider_id": "custom",
                    "api_headers": {"Cookie": "gateway-session"},
                    "api_body": {"chat_template_kwargs": {"enable_thinking": False}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setitem(Settings.model_config, "json_file", path)

        merged = Settings(provider_id="openai", api_headers={}, api_body={}, model="gpt-4o")

        assert merged.api_headers == {"Cookie": "gateway-session"}
        assert merged.api_body == {"chat_template_kwargs": {"enable_thinking": False}}

    def test_saving_the_answers_replaces_the_previous_endpoint(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the wizard does instead: write the answers, then read the settings back."""
        path = tmp_path / "config.json"
        monkeypatch.setattr("bismuth.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("bismuth.config.CONFIG_FILE", path)
        monkeypatch.setitem(Settings.model_config, "json_file", path)
        save_user_config(
            answers(
                provider_id="custom",
                api_key="",
                api_base="http://gateway.internal/v1",
                api_headers={"Cookie": "gateway-session"},
                api_body={"chat_template_kwargs": {"enable_thinking": False}},
                native_schema=True,
                model="qwen3-32b",
            )
        )

        save_user_config(answers())
        reloaded = Settings()

        assert reloaded.provider_id == "openai"
        assert reloaded.api_headers == {}
        assert reloaded.api_body == {}
        assert reloaded.api_base is None
        assert reloaded.native_schema is None


class TestConfigured:
    """`is_configured` gates the setup wizard, so it must not be optimistic."""

    def test_a_fresh_install_is_not_configured(self, clean_env: None) -> None:
        assert not Settings().is_configured

    def test_a_key_without_models_is_not_enough(self, clean_env: None) -> None:
        assert not Settings(provider_id="openai", api_key="sk-x").is_configured

    def test_a_hosted_provider_without_a_key_is_not_enough(self, clean_env: None) -> None:
        assert not configured(api_key="").is_configured

    def test_a_compatible_endpoint_needs_no_key(self, clean_env: None) -> None:
        """Ollama, vLLM and LM Studio all speak the OpenAI protocol and most want no
        credential; they are one provider now, told apart by the address."""
        assert configured(
            provider_id="custom", api_key="", api_base="http://localhost:11434/v1"
        ).is_configured

    def test_a_compatible_endpoint_still_needs_an_address(self, clean_env: None) -> None:
        assert not configured(provider_id="custom", api_key="", api_base=None).is_configured


class TestModelNames:
    """The user picks a bare name from a dropdown; LiteLLM wants a prefix."""

    def test_the_provider_prefix_is_added(self, clean_env: None) -> None:
        settings = configured()
        assert settings.model_for() == "openai/gpt-4o"

    def test_an_already_qualified_name_is_left_alone(self, clean_env: None) -> None:
        settings = configured(model="openrouter/meta/llama-3")
        assert settings.model_for() == "openrouter/meta/llama-3"

    def test_a_compatible_endpoint_is_addressed_as_openai(self, clean_env: None) -> None:
        """LiteLLM routes by protocol, and these speak OpenAI's."""
        settings = configured(provider_id="custom", api_key="", model="qwen3.6-35b")
        assert settings.model_for() == "openai/qwen3.6-35b"

    def test_a_legacy_pair_migrates_to_the_judgement_model(self, clean_env: None) -> None:
        settings = Settings(
            provider_id="openai",
            api_key="sk-test",
            model_fast="gpt-4o-mini",
            model_reasoning="gpt-4o",
        )
        assert settings.model == "gpt-4o"


class TestLocality:
    """runs_locally is computed from the endpoint, not assumed."""

    def test_a_local_address_is_local(self, clean_env: None) -> None:
        """Read off the address, never assumed from the provider's name -- the same
        software is local on this machine and not local on someone else's."""
        assert configured(
            provider_id="custom", api_key="", api_base="http://localhost:11434/v1"
        ).runs_locally

    def test_a_localhost_endpoint_is_local_whatever_the_provider_is_called(
        self, clean_env: None
    ) -> None:
        # vLLM/LM Studio use the OpenAI protocol under a custom provider id.
        assert configured(provider_id="custom", api_base="http://localhost:8000/v1").runs_locally

    def test_a_hosted_provider_is_not_local(self, clean_env: None) -> None:
        assert not configured().runs_locally


class TestRedaction:
    def test_the_key_is_never_rendered_in_full(self, clean_env: None) -> None:
        redacted = configured(api_key="sk-supersecret-abcd").redacted()
        assert redacted["api_key"] == "…abcd"
        assert "supersecret" not in json.dumps(redacted)


class TestProviders:
    def test_every_provider_declares_what_it_needs(self) -> None:
        for entry in PROVIDERS:
            assert entry.litellm_prefix
            assert entry.label
            if entry.needs_api_base:
                assert entry.default_api_base, (
                    f"{entry.id} would leave the wizard with a blank field"
                )

    def test_lookup_of_an_unknown_provider_is_none_not_a_crash(self) -> None:
        assert provider("nope") is None


class TestImportOrdering:
    """litellm must be imported lazily: `import litellm` calls load_dotenv() at module scope."""

    def test_importing_the_adapter_does_not_import_litellm(self) -> None:
        # Subprocess: import side effects can't be undone in-process.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import bismuth.adapters.llm.litellm_adapter; "
                "print('litellm' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert result.stdout.strip() == "False", (
            "Importing the adapter dragged litellm in, which scavenges a .env from "
            "above the virtualenv before we can load ours. Keep the import inside "
            "_load_litellm()."
        )

    def test_importing_the_cli_does_not_import_litellm(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import bismuth.cli.main; print('litellm' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert result.stdout.strip() == "False"

    def test_the_env_file_in_the_working_directory_wins(self, tmp_path: Path) -> None:
        """End to end through the real CLI."""
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / ".env").write_text("BISMUTH_VAULT_PATH=./WRONG\n", encoding="utf-8")

        workdir = decoy / "work"
        workdir.mkdir()
        (workdir / ".env").write_text("BISMUTH_VAULT_PATH=./RIGHT\n", encoding="utf-8")

        # Strip BISMUTH_* from the environment so a leaked var can't decide the outcome.
        clean = {k: v for k, v in os.environ.items() if not k.startswith("BISMUTH_")}
        result = subprocess.run(
            [sys.executable, "-m", "bismuth.cli.main", "doctor"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**clean, "PYTHONIOENCODING": "utf-8", "COLUMNS": "200"},
        )
        assert "RIGHT" in result.stdout, result.stdout
        assert "WRONG" not in result.stdout
