"""Chat history: written down as it is answered, listed, reopened, and deleted."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.adapters.transcripts import FileTranscripts
from bismuth.agentkit.testing import FakeModel, says
from bismuth.api.app import create_app
from bismuth.config import Settings
from bismuth.container import Bismuth, build
from bismuth.domain.transcript import Transcript, TranscriptTurn
from bismuth.services.conversation import ConversationService


def _service(engine: Bismuth, model: object, store: object | None = None) -> ConversationService:
    return ConversationService(
        model=model,  # type: ignore[arg-type]
        vault=engine.vault,
        charters=engine.charters,
        transcripts=store or engine.transcripts,  # type: ignore[arg-type]
    )


@pytest.fixture
def chat_client(settings: Settings, llm: FakeLLM) -> Iterator[TestClient]:
    """A client whose answering model is scripted, so /api/chat never leaves the machine."""
    app = create_app(settings)
    app.state.engine = build(
        settings, llm=llm, chat_model=FakeModel(handler=lambda *_: says("근거 있는 답변"))
    )
    with TestClient(app) as test_client:
        yield test_client


class TestFileTranscripts:
    def test_a_saved_conversation_reads_back_whole(self, tmp_path: Path) -> None:
        store = FileTranscripts(tmp_path)
        store.save(Transcript(id="abc123", turns=[TranscriptTurn(question="질문", answer="답변")]))

        found = store.get("abc123")

        assert found is not None
        assert found.turns[0].question == "질문"
        assert found.title == "질문"

    def test_history_is_listed_newest_answer_first(self, tmp_path: Path) -> None:
        store = FileTranscripts(tmp_path)
        first = Transcript(id="older", turns=[TranscriptTurn(question="먼저", answer="a")])
        second = Transcript(id="newer", turns=[TranscriptTurn(question="나중", answer="b")])
        second.updated_at = second.updated_at.replace(year=second.updated_at.year + 1)
        store.save(first)
        store.save(second)

        assert [s.id for s in store.list()] == ["newer", "older"]
        assert [s.id for s in store.list(limit=1)] == ["newer"]

    def test_a_deleted_conversation_is_gone_and_deleting_twice_is_fine(
        self, tmp_path: Path
    ) -> None:
        store = FileTranscripts(tmp_path)
        store.save(Transcript(id="abc123"))

        store.delete("abc123")
        store.delete("abc123")

        assert store.get("abc123") is None
        assert store.list() == []

    def test_a_conversation_id_cannot_escape_its_directory(self, tmp_path: Path) -> None:
        store = FileTranscripts(tmp_path)

        with pytest.raises(ValueError):
            store.get("../../etc/passwd")

    def test_an_unreadable_file_is_skipped_rather_than_failing_the_list(
        self, tmp_path: Path
    ) -> None:
        store = FileTranscripts(tmp_path)
        store.save(Transcript(id="good", turns=[TranscriptTurn(question="질문", answer="답")]))
        (tmp_path / "conversations" / "broken.json").write_text("{oops", encoding="utf-8")

        assert [s.id for s in store.list()] == ["good"]


class TestConversationHistory:
    async def test_each_answered_turn_is_written_down(self, engine: Bismuth) -> None:
        service = _service(engine, FakeModel(handler=lambda *_: says("답변")))
        conversation, _ = await service.ask("계약 기간이 얼마야?")

        history = service.history()

        assert [s.id for s in history] == [conversation.id]
        assert history[0].title == "계약 기간이 얼마야?"
        assert history[0].turns == 1

    async def test_a_conversation_reopens_after_the_process_that_held_it(
        self, engine: Bismuth
    ) -> None:
        first = _service(engine, FakeModel(handler=lambda *_: says("답변")))
        conversation, _ = await first.ask("금융 문서 있어?")

        # A second service shares only the store, the way a restarted process would.
        seen: list[list[str]] = []

        def watch(system: str, messages, tools):  # type: ignore[no-untyped-def]
            seen.append([f"{m.role}:{m.content}" for m in messages])
            return says("이어진 답변")

        later = _service(engine, FakeModel(handler=watch))
        again, _ = await later.ask("그중 최신은?", conversation_id=conversation.id)

        assert again.id == conversation.id
        assert seen[0] == ["user:금융 문서 있어?", "assistant:답변", "user:그중 최신은?"]
        assert len(later.history()[0].id) == len(conversation.id)

    async def test_forgetting_removes_it_from_history_too(self, engine: Bismuth) -> None:
        service = _service(engine, FakeModel(handler=lambda *_: says("답변")))
        conversation, _ = await service.ask("질문")

        service.forget(conversation.id)

        assert service.history() == []
        assert service.get(conversation.id) is None


class TestChatHistoryApi:
    def test_history_lists_and_reads_back_a_conversation(self, chat_client: TestClient) -> None:
        answered = chat_client.post("/api/chat", json={"message": "계약 기간이 얼마야?"})
        assert answered.status_code == 200
        conversation_id = _conversation_id(answered.text)

        listed = chat_client.get("/api/chat/conversations").json()
        one = chat_client.get(f"/api/chat/conversations/{conversation_id}").json()

        assert [c["id"] for c in listed] == [conversation_id]
        assert listed[0]["title"] == "계약 기간이 얼마야?"
        assert one["turns"][0]["question"] == "계약 기간이 얼마야?"
        assert one["turns"][0]["answer"] == "근거 있는 답변"

    def test_reading_a_conversation_that_is_not_there_is_a_404(
        self, chat_client: TestClient
    ) -> None:
        assert chat_client.get("/api/chat/conversations/nosuchid").status_code == 404

    def test_deleting_a_conversation_drops_it_from_history(self, chat_client: TestClient) -> None:
        answered = chat_client.post("/api/chat", json={"message": "질문"})
        conversation_id = _conversation_id(answered.text)

        chat_client.delete(f"/api/chat/{conversation_id}")

        assert chat_client.get("/api/chat/conversations").json() == []
        assert chat_client.get(f"/api/chat/conversations/{conversation_id}").status_code == 404


def _conversation_id(stream: str) -> str:
    for line in stream.splitlines():
        if line.startswith("data: "):
            event = json.loads(line.removeprefix("data: "))
            if event.get("type") == "answer":
                return str(event["conversation_id"])
    raise AssertionError("the stream carried no answer")
