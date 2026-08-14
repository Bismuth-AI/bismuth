"""The librarian agent driving the real vault tools (agentkit loop, scripted model)."""

from __future__ import annotations

from pathlib import PurePosixPath

from agentkit.testing import FakeModel, call, says

from bismuth.container import Bismuth
from bismuth.services.agent import (
    AgentService,
    _boundary_parent,
    _document_handles,
    _stored_folder,
    build_read_tools,
)
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
    assert {t.name for t in tools} == {
        "ls",
        "tree",
        "inventory",
        "read",
        "grep",
        "read_note",
        "related",
    }
    assert all(getattr(t, "read_only", False) for t in tools)


def test_root_boundary_stays_canonical_between_validations() -> None:
    for spelling in ("", "/", "."):
        root = _boundary_parent(spelling)

        assert root is not None
        assert not root.parts
        assert _stored_folder(root) == ""


async def test_read_tools_do_not_share_a_fixed_call_budget(engine: Bismuth) -> None:
    tools = build_read_tools(engine.vault, engine.charters)
    tree = next(tool for tool in tools if tool.name == "tree")
    inventory = next(tool for tool in tools if tool.name == "inventory")

    await tree.run(tree.params())  # type: ignore[attr-defined]
    inventory_result = await inventory.run(inventory.params())  # type: ignore[attr-defined]

    assert "budget exhausted" not in inventory_result


async def test_ls_surfaces_document_types(engine: Bismuth) -> None:
    # The organizer judges by real doc types (not the self-healing folder note),
    # so ls must expose each document's type.
    await add(engine, "contract.txt")  # scripted card doc_type = "계약서"
    ls = next(t for t in build_read_tools(engine.vault, engine.charters) if t.name == "ls")

    listing = await ls.run(ls.params(path="아폴로/2023"))  # type: ignore[attr-defined]

    assert "contract.txt" in listing
    assert "[계약서]" in listing


async def test_read_tools_accept_display_style_leading_slash(engine: Bismuth) -> None:
    await add(engine, "contract.txt")
    tools = {tool.name: tool for tool in build_read_tools(engine.vault, engine.charters)}

    listing = await tools["ls"].run(tools["ls"].params(path="/아폴로/2023"))
    tree = await tools["tree"].run(tools["tree"].params(path="/아폴로"))

    assert "contract.txt" in listing
    assert "2023/" in tree


async def test_compact_inventory_uses_the_real_catalog_summary(engine: Bismuth) -> None:
    await add(engine, "contract.txt")
    inventory = next(
        tool
        for tool in build_read_tools(
            engine.vault, engine.charters, catalog=engine.catalog
        )
        if tool.name == "inventory"
    )

    output = await inventory.run(inventory.params(path="", recursive=True))

    assert "SUMMARY=대한물산과 유엔진 간" in output


def test_focused_evidence_excludes_loose_backlog_but_keeps_committed_shelves(
    engine: Bismuth,
) -> None:
    service = AgentService(
        model=FakeModel([]),
        vault=engine.vault,
        charters=engine.charters,
        catalog=engine.catalog,
    )
    all_handles = {
        "D000001": PurePosixPath("new.txt"),
        "D000002": PurePosixPath("unprocessed.txt"),
        "D000003": PurePosixPath("금융/committed.txt"),
    }

    evidence = service._evidence_handles(
        all_handles,
        {"D000001": PurePosixPath("new.txt")},
        scope=PurePosixPath(),
        focused=True,
    )

    assert set(evidence) == {"D000001", "R000001"}
    assert evidence["R000001"] == PurePosixPath("금융/committed.txt")


async def test_restricted_read_tools_cannot_leak_or_open_documents_outside_window(
    engine: Bismuth,
) -> None:
    await add(engine, "visible.txt", "VISIBLE-WINDOW-EVIDENCE")
    await add(engine, "hidden.txt", "HIDDEN-BACKLOG-EVIDENCE")
    all_handles = _document_handles(engine.vault)
    visible_handle, visible_path = next(
        (handle, path) for handle, path in all_handles.items() if path.name == "visible.txt"
    )
    hidden_handle, hidden_path = next(
        (handle, path) for handle, path in all_handles.items() if path.name == "hidden.txt"
    )
    tools = {
        tool.name: tool
        for tool in build_read_tools(
            engine.vault,
            engine.charters,
            handles={visible_handle: visible_path},
            restrict_documents=True,
        )
    }

    listing = await tools["ls"].run(tools["ls"].params(path=str(visible_path.parent)))
    inventory = await tools["inventory"].run(
        tools["inventory"].params(path=str(visible_path.parent))
    )
    grep = await tools["grep"].run(tools["grep"].params(pattern="HIDDEN-BACKLOG-EVIDENCE"))
    raw_read = await tools["read"].run(tools["read"].params(path=str(hidden_path)))
    handle_read = await tools["read"].run(tools["read"].params(path=hidden_handle))

    assert "visible.txt" in listing
    assert "hidden.txt" not in listing
    assert visible_handle in inventory
    assert hidden_handle not in inventory
    assert grep == "(no matches)"
    assert "inaccessible" in raw_read
    assert "inaccessible" in handle_read


def _scripted_grep() -> FakeModel:
    return FakeModel(
        [
            says("찾아볼게요", call("grep", {"pattern": "24개월"})),
            says("계약 기간은 24개월입니다."),
        ]
    )
