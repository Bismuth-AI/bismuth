"""The librarian agent driving the real vault tools (agentkit loop, scripted model)."""

from __future__ import annotations

from agentkit.testing import FakeModel, call, says

from bismuth.container import Bismuth
from bismuth.services.agent import AgentService, build_read_tools
from tests.test_ingest import add


def _svc(engine: Bismuth, model: FakeModel) -> AgentService:
    return AgentService(model=model, vault=engine.vault, charters=engine.charters)


async def test_agent_greps_a_sidecar_and_answers(engine: Bismuth) -> None:
    await add(engine, "contract.txt", "계약 기간은 24개월로 한다. 고유 문구 ZZZ.")
    svc = _svc(engine, _scripted_grep())

    result = await svc.ask("계약 기간이 얼마야?")

    assert result.text == "계약 기간은 24개월입니다."
    grep_output = next(m.content for m in result.messages if m.role == "tool")
    assert "24개월" in grep_output  # found in the document's sidecar text
    assert "contract.txt" in grep_output  # cited by document path


async def test_agent_reads_a_document(engine: Bismuth) -> None:
    await add(engine, "contract.txt", "고유 문구 QQQ 계약 내용.")
    turns = [
        says("읽어볼게요", call("read", {"path": "아폴로/2023/contract.txt"})),
        says("문서에 'QQQ'가 있습니다."),
    ]
    svc = _svc(engine, FakeModel(turns))

    result = await svc.ask("문서 내용 알려줘")

    read_output = next(m.content for m in result.messages if m.role == "tool")
    assert "QQQ" in read_output


def test_read_tools_are_all_read_only(engine: Bismuth) -> None:
    tools = build_read_tools(engine.vault, engine.charters)
    assert {t.name for t in tools} == {"ls", "tree", "inventory", "read", "grep", "read_note"}
    assert all(getattr(t, "read_only", False) for t in tools)


async def test_ls_surfaces_document_types(engine: Bismuth) -> None:
    # The organizer judges by real doc types (not the self-healing folder note),
    # so ls must expose each document's type.
    await add(engine, "contract.txt")  # scripted card doc_type = "계약서"
    ls = next(t for t in build_read_tools(engine.vault, engine.charters) if t.name == "ls")

    listing = await ls.run(ls.params(path="아폴로/2023"))  # type: ignore[attr-defined]

    assert "contract.txt" in listing
    assert "[계약서]" in listing


def _scripted_grep() -> FakeModel:
    return FakeModel(
        [
            says("찾아볼게요", call("grep", {"pattern": "24개월"})),
            says("계약 기간은 24개월입니다."),
        ]
    )
