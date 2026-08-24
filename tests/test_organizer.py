"""The organize agent: it proposes a reorganisation; the user approves; bismuth applies."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from bismuth.agentkit.testing import FakeModel, call, says
from bismuth.api.app import create_app
from bismuth.config import Settings
from bismuth.container import Bismuth, build
from bismuth.services.agent import AgentService, build_propose_move_tool
from tests.conftest import seed_folder
from tests.helpers import add


def _svc(engine: Bismuth, model: FakeModel) -> AgentService:
    return AgentService(model=model, vault=engine.vault, charters=engine.charters)


async def test_propose_records_moves_without_touching_disk(engine: Bismuth) -> None:
    await add(engine, "a.txt", "아폴로 계약 A")
    model = FakeModel(
        [
            says("구조를 봅니다", call("tree", {})),
            says(
                "계약서를 나눕니다",
                call("move", {"paths": ["아폴로/2023/a.txt"], "target": "아폴로/2023/계약"}),
            ),
            says("계약 문서를 아폴로/2023/계약 으로 나누자고 제안합니다."),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert [(m.paths, m.target) for m in proposal.moves] == [
        (["아폴로/2023/a.txt"], "아폴로/2023/계약")
    ]
    assert proposal.summary
    # A proposal moves nothing on its own.
    assert (engine.vault.root / "아폴로/2023/a.txt").is_file()
    assert not (engine.vault.root / "아폴로/2023/계약").exists()


async def test_propose_can_recommend_no_change(engine: Bismuth) -> None:
    await add(engine, "a.txt", "아폴로 계약 A")
    model = FakeModel([says("", call("tree", {})), says("구조가 이미 명확합니다. 바꿀 것 없음.")])

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.moves == []
    assert "바꿀 것 없음" in proposal.summary


def test_propose_move_tool_only_records(engine: Bismuth) -> None:
    sink: list[object] = []
    tool = build_propose_move_tool(sink)  # type: ignore[arg-type]
    assert tool.read_only is True  # type: ignore[attr-defined]


def test_organize_api_propose_then_apply(settings: Settings, llm: object) -> None:
    chat = FakeModel(
        [
            says("", call("move", {"paths": ["아폴로/2023/a.txt"], "target": "아폴로/2023/계약"})),
            says("계약 폴더로 나누자고 제안합니다."),
        ]
    )
    app = create_app(settings, accepted_uploads=frozenset({".txt"}))
    organized = build(settings, llm=llm, chat_model=chat)  # type: ignore[arg-type]
    seed_folder(Path(organized.vault.root))
    asyncio.run(add(organized, "a.txt", "아폴로 계약 A"))
    app.state.engine = organized

    with TestClient(app) as client:
        plan = client.post("/api/organize/propose", json={}).json()
        assert plan["moves"] == [{"paths": ["아폴로/2023/a.txt"], "target": "아폴로/2023/계약"}]

        applied = client.post("/api/organize/apply", json={"moves": plan["moves"]}).json()
        assert applied["applied"] == 1

        paths = [f["path"] for f in client.get("/api/tree").json()]
        assert "아폴로/2023/계약" in paths  # the approved plan reshaped the tree


def test_organize_api_applies_a_rename(settings: Settings, llm: object) -> None:
    chat = FakeModel(
        [
            says("", call("rename", {"folder": "아폴로/2023", "new_name": "이천이십삼"})),
            says("폴더 이름이 내용과 안 맞아 바꾸자고 제안합니다."),
        ]
    )
    app = create_app(settings, accepted_uploads=frozenset({".txt"}))
    organized = build(settings, llm=llm, chat_model=chat)  # type: ignore[arg-type]
    seed_folder(Path(organized.vault.root))
    asyncio.run(add(organized, "a.txt", "아폴로 계약 A"))
    app.state.engine = organized

    with TestClient(app) as client:
        plan = client.post("/api/organize/propose", json={}).json()
        assert plan["renames"] == [{"folder": "아폴로/2023", "new_name": "이천이십삼"}]

        client.post("/api/organize/apply", json={"renames": plan["renames"]})

        paths = [f["path"] for f in client.get("/api/tree").json()]
        assert "아폴로/이천이십삼" in paths
        assert "아폴로/2023" not in paths
