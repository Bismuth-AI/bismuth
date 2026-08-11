"""Shared fixtures: a temp vault and engine backed by a scripted model."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from bismuth import logging_setup
from bismuth.adapters.llm.fake import FakeLLM
from bismuth.api.app import create_app
from bismuth.config import Settings
from bismuth.container import Bismuth, build
from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.document import Entity, EntityKind
from bismuth.ports.llm import Prompt
from bismuth.prompts import cards as card_prompts
from bismuth.prompts import charters as charter_prompts
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import subdivision as subdivision_prompts


@pytest.fixture(autouse=True)
def _logs_go_somewhere_disposable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite out of the repository's ./logs.

    LOG_DIR is relative, and anything that builds an app configures logging on startup --
    so running the tests truncated whatever run was being investigated at the time. The
    logs are the evidence; a test suite must not be able to destroy it.
    """
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def settings(vault_path: Path) -> Settings:
    return Settings(vault_path=vault_path)


def placement_to(folder: str | None, *, confidence: float = 0.9):
    """Return a scripted direct-child chooser that walks to ``folder``."""
    target = PurePosixPath(folder or "")

    def decide(prompt: Prompt, schema: type[BaseModel]) -> BaseModel:
        if folder is None:
            return placement_prompts.PlacementDecision(folder_id=None, confidence=confidence)
        current_line = next(
            line for line in prompt.user.splitlines() if line.startswith("CURRENT FOLDER:")
        )
        current_raw = current_line.partition(":")[2].strip()
        current = PurePosixPath() if current_raw == "(root)" else PurePosixPath(current_raw)
        if target.parts[: len(current.parts)] != current.parts or len(target.parts) <= len(
            current.parts
        ):
            return placement_prompts.PlacementDecision(folder_id="", confidence=confidence)
        wanted = target.parts[len(current.parts)]
        for line in prompt.user.splitlines():
            stripped = line.strip()
            if not stripped.startswith("[F"):
                continue
            folder_id, _, shown = stripped.partition("]")
            name = shown.strip().split(" — ", 1)[0]
            if name == wanted:
                return placement_prompts.PlacementDecision(
                    folder_id=folder_id.removeprefix("["), confidence=confidence
                )
        return placement_prompts.PlacementDecision(folder_id="", confidence=confidence)

    return decide


class ScriptedModel:
    """FakeLLM handler that returns a scripted response keyed by schema."""

    def __init__(self) -> None:
        self.responses: dict[type[BaseModel], Any] = {
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
                summary="대한물산과 유엔진 간 아폴로 유지보수 계약. 기간 24개월, 지연배상 조항 포함.",
            ),
            placement_prompts.PlacementDecision: placement_to("아폴로/2023"),
            # Default: nothing has grown out yet. A test that wants a folder scripts it,
            # so every other test keeps the tree its assertions were written against.
            subdivision_prompts.Emerging: subdivision_prompts.Emerging(
                emerged=False,
            ),
            subdivision_prompts.Members: subdivision_prompts.Members(
                document_ids=[],
            ),
            subdivision_prompts.Review: subdivision_prompts.Review(
                one_axis=True,
                coherent_membership=True,
                useful_navigation=True,
            ),
            subdivision_prompts.Replacement: subdivision_prompts.Replacement(
                basis="",
                basis_question="",
                groups=[],
            ),
            subdivision_prompts.ReplacementSketch: subdivision_prompts.ReplacementSketch(
                basis="문서 종류",
                basis_question="이 문서의 종류는 무엇인가?",
                signs=[
                    subdivision_prompts.ReplacementSign(name="자료", note="자료 문서"),
                    subdivision_prompts.ReplacementSign(name="기록", note="기록 문서"),
                ],
            ),
            subdivision_prompts.ReplacementAssignments: (
                subdivision_prompts.ReplacementAssignments(groups=[])
            ),
            subdivision_prompts.BoundaryAudit: subdivision_prompts.BoundaryAudit(
                one_property=True,
                names_answer_question=True,
                mutually_exclusive=True,
                useful_for_navigation=True,
                notes_are_routing_signs=True,
            ),
            subdivision_prompts.ReplacementAudit: subdivision_prompts.ReplacementAudit(
                fixes_observed_failure=True,
                better_navigation=True,
            ),
            subdivision_prompts.ExistingAssignments: subdivision_prompts.ExistingAssignments(
                groups=[],
            ),
            subdivision_prompts.RoutingAudit: subdivision_prompts.RoutingAudit(
                assignments_match_signs=True,
                no_forced_fit=True,
            ),
            charter_prompts.CharterDraft: charter_prompts.CharterDraft(
                purpose="아폴로 사업의 2023년 문서를 모아둡니다.",
            ),
        }

    def set(self, schema: type[BaseModel], response: object) -> None:
        self.responses[schema] = response

    def __call__(self, prompt: Prompt, schema: type[BaseModel]) -> BaseModel:
        try:
            response = self.responses[schema]
            return response(prompt, schema) if callable(response) else response
        except KeyError as exc:  # pragma: no cover
            raise AssertionError(f"nothing scripted for {schema.__name__}") from exc


@pytest.fixture
def script() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def llm(script: ScriptedModel) -> FakeLLM:
    return FakeLLM(handler=script)


SCRIPTED_FOLDER = PurePosixPath("아폴로/2023")


def seed_folder(root: Path, folder: PurePosixPath = SCRIPTED_FOLDER) -> None:
    """Put a folder on disk so placement has somewhere to choose.

    Placement chooses; it does not invent (see prompts/placement.py). A scripted
    decision naming a folder that does not exist is read as the root, so a test that
    wants a document filed somewhere has to put the somewhere there first.
    """
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
    app = create_app(settings)
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
