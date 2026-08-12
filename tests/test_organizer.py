"""The organizer submits a complete shadow plan before autonomous application."""

from __future__ import annotations

from pathlib import Path

from agentkit.testing import FakeModel, call, says
from fastapi.testclient import TestClient

from bismuth.api.app import create_app
from bismuth.config import Settings
from bismuth.container import Bismuth, build
from bismuth.services.agent import AgentService
from tests.conftest import seed_folder
from tests.test_ingest import add


def _svc(engine: Bismuth, model: FakeModel) -> AgentService:
    return AgentService(model=model, vault=engine.vault, charters=engine.charters)


def _plan_call() -> object:
    return call(
        "submit_plan",
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "axis": "문서 성격",
                    "axis_question": "이 문서는 어떤 성격인가?",
                    "moves": [
                        {
                            "document_ids": ["D000001", "D000002"],
                            "target": "아폴로/2023/계약",
                        },
                        {
                            "document_ids": ["D000003", "D000004"],
                            "target": "아폴로/2023/보고",
                        },
                    ],
                }
            ]
        },
    )


async def _four_documents(engine: Bismuth) -> None:
    for index, name in enumerate(("a.txt", "b.txt", "c.txt", "d.txt"), start=1):
        await add(engine, name, f"서로 다른 문서 {index}")


async def test_shadow_plan_is_validated_without_touching_disk(engine: Bismuth) -> None:
    await _four_documents(engine)
    model = FakeModel([says("완성안을 제출합니다", _plan_call()), says("두 경계를 제안합니다.")])

    proposal = await _svc(engine, model).propose_reorg()

    assert [(move.paths, move.target) for move in proposal.moves] == [
        (["아폴로/2023/a.txt", "아폴로/2023/b.txt"], "아폴로/2023/계약"),
        (["아폴로/2023/c.txt", "아폴로/2023/d.txt"], "아폴로/2023/보고"),
    ]
    assert proposal.problems == []
    assert (engine.vault.root / "아폴로/2023/a.txt").is_file()
    assert not (engine.vault.root / "아폴로/2023/계약").exists()


async def test_propose_can_recommend_no_change(engine: Bismuth) -> None:
    await add(engine, "a.txt", "아폴로 계약 A")
    model = FakeModel(
        [
            says("", call("tree", {})),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "구조가 이미 명확합니다. 바꿀 것이 없습니다."},
                ),
            ),
            says("검토를 마쳤습니다."),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.moves == []
    assert "바꿀 것이 없습니다" in proposal.summary


async def test_turn_exhaustion_is_not_reported_as_a_successful_no_change(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    model = FakeModel(
        [says("", call("tree", {}, call_id=f"turn-{index}")) for index in range(24)]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.moves == []
    assert "exhausted" in " ".join(proposal.problems)


async def test_arrival_window_exposes_only_focus_cards_with_short_handles(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    document_ids = [document_id for document_id, _ in engine.catalog.iter_cards()]
    model = FakeModel(
        [
            says("", call("arrivals", {})),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "두 도착 문서만으로는 기존 경계를 바꿀 근거가 없습니다."},
                ),
            ),
            says("검토를 마쳤습니다."),
        ]
    )

    await _svc(engine, model).propose_reorg(focus_document_ids=document_ids[:2])

    arrival_output = next(message.content for message in model.calls[1][1] if message.role == "tool")
    assert arrival_output.count("ID=D") == 2
    assert all(document_id not in arrival_output for document_id in document_ids)


def test_completed_upload_set_applies_valid_shadow_plan_automatically(
    settings: Settings, llm: object
) -> None:
    chat = FakeModel([says("완성안을 제출합니다", _plan_call()), says("적용 가능합니다.")])
    app = create_app(settings)
    organized = build(settings, llm=llm, chat_model=chat)  # type: ignore[arg-type]
    seed_folder(Path(organized.vault.root))
    app.state.engine = organized

    files = [
        ("files", (name, f"서로 다른 문서 {index}".encode(), "text/plain"))
        for index, name in enumerate(("a.txt", "b.txt", "c.txt", "d.txt"), start=1)
    ]
    with TestClient(app) as client:
        response = client.post("/api/documents", files=files)

        assert response.status_code == 200
        paths = [folder["path"] for folder in client.get("/api/tree").json()]
        assert "아폴로/2023/계약" in paths
        assert "아폴로/2023/보고" in paths
