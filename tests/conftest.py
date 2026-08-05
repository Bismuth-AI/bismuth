"""Shared fixtures: a temp vault and engine backed by a scripted model."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.config import Settings
from bismuth.container import Bismuth, build
from bismuth.domain.document import Entity, EntityKind
from bismuth.ports.llm import ModelProfile, Prompt
from bismuth.prompts import cards as card_prompts
from bismuth.prompts import charters as charter_prompts
from bismuth.prompts import placement as placement_prompts


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def settings(vault_path: Path) -> Settings:
    return Settings(vault_path=vault_path)


class ScriptedModel:
    """FakeLLM handler that returns a scripted response keyed by schema."""

    def __init__(self) -> None:
        self.responses: dict[type[BaseModel], BaseModel] = {
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
                contributed=True,
                note="scripted update",
                new_topics=["지연배상"],
                new_entities=[Entity(name="대한물산", kind=EntityKind.ORGANIZATION)],
                new_keywords=["지연배상금"],
                new_questions=["지연배상금 요율은?"],
            ),
            card_prompts.DensifiedSummary: card_prompts.DensifiedSummary(
                summary="대한물산과 유엔진 간 아폴로 유지보수 계약. 기간 24개월, 지연배상 조항 포함.",
                absorbed=["지연배상"],
            ),
            placement_prompts.PlacementDecision: placement_prompts.PlacementDecision(
                folder="아폴로/2023",
                existing=False,
                confidence=0.9,
                reason="아폴로 사업 2023년 계약서라 아폴로/2023 에 둡니다.",
            ),
            charter_prompts.CharterDraft: charter_prompts.CharterDraft(
                title="아폴로 2023",
                purpose="아폴로 사업의 2023년 문서를 모아둡니다.",
                holds=["아폴로 사업 계약서·보고서"],
                answers=["2023년 아폴로 사업에서 무엇이 합의되었나?"],
            ),
        }

    def set(self, schema: type[BaseModel], response: BaseModel) -> None:
        self.responses[schema] = response

    def __call__(self, prompt: Prompt, schema: type[BaseModel], profile: ModelProfile) -> BaseModel:
        try:
            return self.responses[schema]
        except KeyError as exc:  # pragma: no cover
            raise AssertionError(f"nothing scripted for {schema.__name__}") from exc


@pytest.fixture
def script() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def llm(script: ScriptedModel) -> FakeLLM:
    return FakeLLM(handler=script)


@pytest.fixture
def engine(settings: Settings, llm: FakeLLM) -> Bismuth:
    return build(settings, llm=llm)


@pytest.fixture
def make_document(vault_path: Path) -> Callable[[str, str], Path]:
    def _make(name: str, body: str = "아폴로 지원 계약서, 2023.") -> Path:
        path = vault_path.parent / "incoming" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    return _make
