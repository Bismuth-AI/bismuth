"""Deferred imports are about when, not whether: nothing may still be unimported once
the server is accepting requests."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bismuth.adapters.llm import litellm_adapter
from bismuth.adapters.llm import wire as llm_wire
from bismuth.adapters.parsers import build_registry
from bismuth.adapters.parsers.registry import ExtensionRegistry, require
from bismuth.api.app import create_app
from bismuth.domain.errors import ParserUnavailableError
from bismuth.ports.parser import DocumentParser


class _Missing:
    """A parser whose optional dependency is not installed."""

    @property
    def name(self) -> str:
        return "missing"

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".nope"})

    def warm(self) -> None:
        require("a_module_that_is_not_installed", "install the thing")

    def parse(self, path: Path, *, max_chars: int) -> object:  # pragma: no cover
        raise AssertionError("not reached")


class TestRequire:
    def test_a_present_module_comes_back(self) -> None:
        assert require("json", "unused").__name__ == "json"

    def test_a_missing_module_says_how_to_get_it(self) -> None:
        with pytest.raises(ParserUnavailableError, match="pip install"):
            require("definitely_not_a_module", "pip install 'bismuth-kb[parsers]'")


class TestWarm:
    def test_warming_imports_every_parser_dependency(self) -> None:
        dependencies = {"pypdf", "lxml", "olefile", "unword", "pptx", "openpyxl"}
        # unword is a PyO3 extension which cannot be initialized twice in one
        # interpreter, so unlike the pure-Python dependencies it must stay cached.
        for module in dependencies - {"unword"}:
            sys.modules.pop(module, None)

        assert build_registry().warm() == {}

        # warm() loads every parser dependency before requests arrive.
        assert dependencies <= set(sys.modules)

    def test_every_registered_parser_can_be_warmed(self) -> None:
        # A parser added without warm() would only fail on the first upload of that format.
        registry = build_registry()
        for extension in registry.supported_extensions():
            parser: DocumentParser = registry.for_path(Path(f"x{extension}"))
            parser.warm()

    def test_a_missing_extra_is_reported_not_raised(self) -> None:
        """A minimal install is supported, so an absent parser must not stop the server."""
        registry = ExtensionRegistry([_Missing()])

        unavailable = registry.warm()

        assert list(unavailable) == ["missing"]
        assert "install the thing" in unavailable["missing"]

    def test_warming_twice_is_harmless(self) -> None:
        registry = build_registry()
        assert registry.warm() == registry.warm() == {}


class TestServerPreload:
    def test_litellm_is_loaded_by_the_time_the_server_answers(
        self,
        settings,
        llm,
        tmp_path,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """The adapter is loaded during application startup."""
        monkeypatch.chdir(tmp_path)  # startup writes ./logs
        monkeypatch.setattr(llm_wire, "_litellm", None)
        assert llm_wire._litellm is None

        with TestClient(create_app(settings)):
            assert llm_wire._litellm is not None

    def test_no_parser_import_is_left_for_the_first_upload(self, client) -> None:  # type: ignore[no-untyped-def]
        assert {"pypdf", "lxml", "olefile", "unword", "pptx", "openpyxl"} <= set(sys.modules)


class TestStartupMakesNoNetworkCall:
    """Startup must not fetch remote metadata while importing LiteLLM."""

    def _instant_litellm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "litellm", types.ModuleType("litellm"))
        monkeypatch.setattr(llm_wire, "_litellm", None)

    def test_the_price_list_comes_from_the_installed_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
        self._instant_litellm(monkeypatch)

        litellm_adapter.preload()

        assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "true"

    def test_asking_for_the_current_list_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Opting back in has to be possible, or the price list can never be corrected."""
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "false")
        self._instant_litellm(monkeypatch)

        litellm_adapter.preload()

        assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "false"
