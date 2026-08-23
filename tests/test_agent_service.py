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


async def test_grep_takes_one_file_and_says_where_inside_it(engine: Bismuth) -> None:
    # Finding the place inside a long document, so the next read goes straight there
    # instead of walking it a few lines at a time.
    await add(engine, "contract.txt", "계약 기간은 24개월로 한다. 고유 문구 ZZZ.")
    grep = next(t for t in build_read_tools(engine.vault, engine.charters) if t.name == "grep")

    hits = await grep.run(grep.params(pattern="ZZZ", path="아폴로/2023/contract.txt"))  # type: ignore[attr-defined]

    assert "contract.txt" in hits
    assert "ZZZ" in hits


async def test_grep_on_a_path_that_is_neither_says_so(engine: Bismuth) -> None:
    grep = next(t for t in build_read_tools(engine.vault, engine.charters) if t.name == "grep")

    assert "No such" in await grep.run(grep.params(pattern="x", path="nowhere/at/all"))  # type: ignore[attr-defined]


async def test_grep_groups_hits_under_one_path_per_document(engine: Bismuth) -> None:
    # A hit is a pointer to a place. Repeating the path for every line spends the
    # result's whole budget saying the same thing -- and a result that outgrows the
    # budget gets cut in the middle, losing documents silently.
    body = "\n".join(f"{i}번째 고유 문구 QQQ." for i in range(10))
    await add(engine, "contract.txt", body)
    grep = next(t for t in build_read_tools(engine.vault, engine.charters) if t.name == "grep")

    hits = await grep.run(grep.params(pattern="QQQ"))  # type: ignore[attr-defined]

    paths = [line for line in hits.splitlines() if not line.startswith("  ")]
    assert len(paths) == 1, f"the path should appear once, not per hit: {hits}"
    assert "이 문서에서" in hits, "a document with more hits than shown must say so"


async def test_tree_prints_paths_that_can_be_used(engine: Bismuth) -> None:
    # Indentation alone made the caller rebuild a path by counting spaces, and it got
    # one wrong: it asked for a folder under the wrong parent, got a bare refusal, and
    # spent its whole budget without opening a document.
    await add(engine, "contract.txt")
    tools = {t.name: t for t in build_read_tools(engine.vault, engine.charters)}

    tree = await tools["tree"].run(tools["tree"].params(path=""))  # type: ignore[attr-defined]

    for line in tree.splitlines():
        path = line.split("/  (")[0]
        listing = await tools["ls"].run(tools["ls"].params(path=path))  # type: ignore[attr-defined]
        assert "No such folder" not in listing, f"tree printed {path!r}, ls does not know it"


async def test_a_wrong_path_names_the_ones_it_could_have_meant(engine: Bismuth) -> None:
    await add(engine, "contract.txt")  # filed under 아폴로/2023
    tools = {t.name: t for t in build_read_tools(engine.vault, engine.charters)}

    listing = await tools["ls"].run(tools["ls"].params(path="없는폴더/2023"))  # type: ignore[attr-defined]

    assert "No such folder" in listing
    assert "아폴로/2023" in listing, f"no suggestion to recover from: {listing}"


async def test_grep_finds_a_phrase_split_across_a_line_break(engine: Bismuth) -> None:
    # A sidecar is text pulled out of a PDF, hard-wrapped at the page's width. A phrase
    # lands on two lines often enough to matter: the real corpus holds
    # "연 100분" / "의 15를 말한다", which a line-at-a-time search cannot see, and the
    # agent then answers -- correctly, from what it was shown -- that it is not there.
    await add(engine, "contract.txt", "지연배상금의 이율은 연 100분\n의 15로 한다.")
    grep = next(t for t in build_read_tools(engine.vault, engine.charters) if t.name == "grep")

    hits = await grep.run(grep.params(pattern="연 100분의 15"))  # type: ignore[attr-defined]

    assert "contract.txt" in hits, f"the phrase was there, split over two lines: {hits}"


async def test_grep_still_honours_a_line_anchor(engine: Bismuth) -> None:
    # ^ and $ mean per-line; a whitespace-blind pass has no lines to anchor to, so an
    # anchored pattern must keep the line-at-a-time behaviour.
    await add(engine, "contract.txt", "제1조(목적)\n다만 제2조는 예외로 한다.")
    grep = next(t for t in build_read_tools(engine.vault, engine.charters) if t.name == "grep")

    hits = await grep.run(grep.params(pattern=r"^제\d+조"))  # type: ignore[attr-defined]

    assert "제1조" in hits
    assert "다만 제2조" not in hits, "an anchored pattern matched mid-line"


async def test_grep_on_one_file_lists_its_matches_instead_of_counting_them(
    engine: Bismuth,
) -> None:
    # Asking about one document is asking what is in it. Answering "…이 문서에서 40 곳"
    # makes the obvious next question -- which articles does it have? -- unanswerable.
    body = "\n".join(f"제{i}조(어떤 조문) 내용." for i in range(1, 41))
    await add(engine, "contract.txt", body)
    grep = next(t for t in build_read_tools(engine.vault, engine.charters) if t.name == "grep")

    hits = await grep.run(grep.params(pattern="조문", path="아폴로/2023/contract.txt"))  # type: ignore[attr-defined]

    assert hits.count("제") >= 40, f"only a sample came back: {hits[:200]}"
    assert "이 문서에서" not in hits


def test_read_tools_are_all_read_only(engine: Bismuth) -> None:
    tools = build_read_tools(engine.vault, engine.charters)
    assert {t.name for t in tools} == {"ls", "tree", "read", "grep", "read_note"}
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


class TestConversation:
    """Multi-turn: the second question leans on the first, and the tools stay read-only."""

    def _service(self, model, engine):  # type: ignore[no-untyped-def]
        from bismuth.services.conversation import ConversationService

        return ConversationService(model=model, vault=engine.vault, charters=engine.charters)

    async def test_a_follow_up_is_asked_with_what_came_before(self, engine) -> None:  # type: ignore[no-untyped-def]
        from agentkit.testing import FakeModel, says

        seen: list[list[str]] = []

        def watch(system: str, messages, tools):  # type: ignore[no-untyped-def]
            seen.append([f"{m.role}:{m.content}" for m in messages])
            return says("답변")

        service = self._service(FakeModel(handler=watch), engine)
        conversation, first = await service.ask("금융 문서 있어?")
        await service.ask("그중 최신은?", conversation_id=conversation.id)

        assert first == "답변"
        assert seen[1] == ["user:금융 문서 있어?", "assistant:답변", "user:그중 최신은?"]

    async def test_each_conversation_is_its_own_transcript(self, engine) -> None:  # type: ignore[no-untyped-def]
        from agentkit.testing import FakeModel, says

        service = self._service(FakeModel(handler=lambda *_: says("답변")), engine)
        one, _ = await service.ask("첫 질문")
        two, _ = await service.ask("다른 대화")

        assert one.id != two.id
        assert len(two.messages) == 2, "a new conversation starts empty"

    async def test_forgetting_a_conversation_starts_over(self, engine) -> None:  # type: ignore[no-untyped-def]
        from agentkit.testing import FakeModel, says

        service = self._service(FakeModel(handler=lambda *_: says("답변")), engine)
        conversation, _ = await service.ask("질문")
        service.forget(conversation.id)

        assert service.get(conversation.id) is None
        again, _ = await service.ask("질문", conversation_id=conversation.id)
        assert again.id != conversation.id, "a forgotten id gets a fresh conversation"
