"""Shared fixtures for a temporary vault backed by a scripted model."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from bismuth import logging_setup
from bismuth.adapters.llm import ModelProbe
from bismuth.adapters.llm.fake import FakeLLM
from bismuth.api.app import create_app
from bismuth.config import Settings
from bismuth.container import Bismuth, build
from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.document import Entity, EntityKind
from bismuth.ports.llm import Prompt
from bismuth.prompts import cards as card_prompts
from bismuth.prompts import charters as charter_prompts


@pytest.fixture(autouse=True)
def _logs_go_somewhere_disposable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")


@pytest.fixture(autouse=True)
def _the_setup_probe_answers_without_a_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Saving settings calls the chosen model for real. No test may leave this machine."""
    monkeypatch.setattr(
        "bismuth.api.app.probe_model", lambda *_, **__: ModelProbe(ok=True), raising=True
    )


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture(autouse=True)
def _config_stays_out_of_this(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path_factory.mktemp("bismuth-home")
    monkeypatch.setattr("bismuth.config.CONFIG_DIR", home)
    monkeypatch.setattr("bismuth.config.CONFIG_FILE", home / "config.json")
    monkeypatch.setitem(Settings.model_config, "json_file", home / "config.json")


@pytest.fixture
def settings(vault_path: Path) -> Settings:
    return Settings(vault_path=vault_path)


def _as_lines(card: BaseModel) -> str:
    lines: list[str] = []
    for tag, field in (("TITLE", "title"), ("DOCTYPE", "doc_type"), ("LANGUAGE", "language")):
        if value := (getattr(card, field, "") or "").strip():
            lines.append(f"{tag}: {value}")
    lines.append(f"SUMMARY: {getattr(card, 'summary', '')}")
    for tag, field in (
        ("TOPIC", "topics"),
        ("KEYWORD", "keywords"),
        ("QUESTION", "answers_questions"),
        ("TOPIC", "new_topics"),
        ("KEYWORD", "new_keywords"),
        ("QUESTION", "new_questions"),
    ):
        for item in getattr(card, field, ()) or ():
            lines.append(f"{tag}: {item}")
    for field in ("entities", "new_entities"):
        for entity in getattr(card, field, ()) or ():
            lines.append(f"ENTITY: {entity.name} | {entity.kind.value}")
    return "\n".join(lines)


class ScriptedModel:
    """Return test responses keyed by their response model."""

    def __init__(self) -> None:
        self.responses: dict[type[BaseModel] | None, Any] = {
            card_prompts.CardDraft: card_prompts.CardDraft(
                title="아폴로 지원 계약서",
                summary="대한물산과 유엔진 간의 아폴로 사업 유지보수 계약.",
                doc_type="계약서",
                language="ko",
                topics=["아폴로", "유지보수", "2023"],
                entities=[Entity(name="아폴로", kind=EntityKind.PROJECT)],
                keywords=["계약", "24개월"],
                answers_questions=["아폴로 계약 기간은?"],
            ),
            card_prompts.CardUpdate: card_prompts.CardUpdate(
                summary="대한물산과 유엔진 간의 아폴로 사업 유지보수 계약. 지연배상 조항을 포함한다.",
                new_topics=["지연배상"],
                new_entities=[Entity(name="대한물산", kind=EntityKind.ORGANIZATION)],
                new_keywords=["지연배상금"],
                new_questions=["지연배상금 요율은?"],
            ),
            card_prompts.DensifiedSummary: card_prompts.DensifiedSummary(
                summary="대한물산과 유엔진 간 아폴로 유지보수 계약. 기간 24개월, 지연배상 조항 포함."
            ),
            charter_prompts.CharterDraft: charter_prompts.CharterDraft(
                purpose="아폴로 사업의 2023년 문서를 모아둡니다."
            ),
        }

    def set(self, schema: type[BaseModel], response: object) -> None:
        self.responses[schema] = response

    def __call__(self, prompt: Prompt, schema: type[BaseModel] | None) -> BaseModel | str:
        if schema is None and "Return plain tagged lines" in prompt.system:
            scripted = self.responses[
                card_prompts.CardUpdate
                if "SUMMARY is required" in prompt.system
                else card_prompts.CardDraft
            ]
            if callable(scripted):
                scripted = scripted(prompt, None)
            return scripted if isinstance(scripted, str) else _as_lines(scripted)
        if schema is None:
            return ""
        try:
            response = self.responses[schema]
        except KeyError as exc:
            wanted = schema.__name__ if schema is not None else "OpenText"
            raise AssertionError(f"nothing scripted for {wanted}") from exc
        return response(prompt, schema) if callable(response) else response


@pytest.fixture
def script() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def llm(script: ScriptedModel) -> FakeLLM:
    return FakeLLM(handler=script)


SCRIPTED_FOLDER = PurePosixPath("아폴로/2023")


def seed_folder(root: Path, folder: PurePosixPath = SCRIPTED_FOLDER) -> None:
    target = root / Path(*folder.parts)
    target.mkdir(parents=True, exist_ok=True)
    note = Charter(path=folder, title=folder.name, purpose="아폴로 사업의 2023년 문서.")
    (target / CHARTER_FILENAME).write_text(note.to_markdown(), encoding="utf-8")


@pytest.fixture
def engine(settings: Settings, llm: FakeLLM) -> Bismuth:
    engine = build(settings, llm=llm)
    seed_folder(Path(engine.vault.root))
    return engine


@pytest.fixture
def client(settings: Settings, llm: FakeLLM) -> Iterator[TestClient]:
    app = create_app(settings, accepted_uploads=frozenset({".pdf", ".txt", ".md", ".csv"}))
    engine = build(settings, llm=llm)
    seed_folder(Path(engine.vault.root))
    app.state.engine = engine
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def make_document(vault_path: Path) -> Callable[[str, str], Path]:
    def _make(name: str, body: str = "아폴로 지원 계약서, 2023.") -> Path:
        path = vault_path.parent / "incoming" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    return _make
