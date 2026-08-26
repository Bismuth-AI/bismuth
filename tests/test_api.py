"""HTTP API endpoint tests, exercised through a real TestClient."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.agentkit import AssistantMessage
from bismuth.api import app as api_app
from bismuth.api.app import create_app
from bismuth.cli.main import _is_loopback_host
from bismuth.config import Settings
from bismuth.container import build
from bismuth.ports.llm import CURRENT_USAGE, Usage

from .conftest import seed_folder
from .test_ingest import add


def _seed_document(
    client: TestClient, name: str = "contract.txt", body: str = "아폴로 계약서"
) -> None:
    asyncio.run(add(client.app.state.engine, name, body))  # type: ignore[attr-defined]


class TestIndex:
    def test_ui_shell_is_not_cached_across_server_upgrades(self, client: TestClient) -> None:
        response = client.get("/")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

    def test_batch_success_excludes_duplicates_inbox_and_failures(self, client: TestClient) -> None:
        page = client.get("/").text
        assert "batch.completed - batch.failed - batch.duplicate - batch.inbox" in page

    def test_every_picker_upload_uses_the_batch_pipeline(self, client: TestClient) -> None:
        page = client.get("/").text

        assert 'api("/batches", { method: "POST", body: fd })' in page
        assert 'api("/documents",' not in page

    def test_picker_lists_every_supported_document_format(self, client: TestClient) -> None:
        page = client.get("/").text

        assert 'accept=".pdf,.hwp,.hwpx,.doc,.docx,.txt,.md"' in page
        assert "PDF · HWP · HWPX · DOC · DOCX · TXT · MD" in page

    def test_rebuild_controls_are_available(self, client: TestClient) -> None:
        page = client.get("/").text

        assert 'id="btn-empty-tree"' in page
        assert 'id="btn-refile"' in page

    def test_open_vault_control_is_available(self, client: TestClient) -> None:
        page = client.get("/").text

        assert 'id="btn-open-vault"' in page
        assert 'api("/vault/open", { method: "POST" })' in page

    def test_primary_header_keeps_maintenance_actions_in_a_menu(self, client: TestClient) -> None:
        page = client.get("/").text

        assert 'id="btn-manage"' in page
        assert "카드로 전체 재분류" in page
        assert "폴더 구조 비우기" in page

    def test_document_view_defaults_to_compact_and_can_be_expanded(
        self, client: TestClient
    ) -> None:
        page = client.get("/").text

        assert 'localStorage.getItem("bismuth-document-view")' in page
        assert '"자세히 보기"' in page
        assert "-webkit-line-clamp: 2" in page

    def test_large_folders_are_loaded_one_bounded_page_at_a_time(self, client: TestClient) -> None:
        page = client.get("/").text

        assert "const FOLDER_PAGE_SIZE = 100" in page
        assert "function bindMoreButton()" in page
        assert 'id="load-more"' in page

    def test_chat_map_has_bounded_visual_elements(self, client: TestClient) -> None:
        page = client.get("/chat").text

        assert "const MAP_MAX_SHELVES = 24" in page
        assert "const MAP_MAX_SPINES_PER_SHELF = 96" in page

    def test_search_controls_are_available(self, client: TestClient) -> None:
        page = client.get("/").text

        assert 'id="vault-search"' in page
        assert 'api("/search?q=" + encodeURIComponent(query))' in page
        assert "data-result-folder=" in page

    def test_demo_routes_are_not_part_of_the_product(self, client: TestClient) -> None:
        assert client.get("/demo").status_code == 404
        assert client.get("/demo/chat").status_code == 404

    def test_static_html_escapes_attribute_quotes(self, client: TestClient) -> None:
        assert "/[&<>\"']/g" in client.get("/chat").text
        assert "/[&<>\"']/g" in client.get("/trace").text


class TestStatus:
    def test_status_answers(self, client: TestClient) -> None:
        assert client.get("/api/status").status_code == 200

    def test_status_reports_the_shape_of_the_vault(self, client: TestClient) -> None:
        body = client.get("/api/status").json()
        assert body["documents"] == 0
        # The seeded parent and child folders provide an existing filing destination.
        assert body["folders"] == 2
        assert "runs_locally" in body

    def test_status_reports_web_upload_formats(self, settings: Settings) -> None:
        with TestClient(create_app(settings)) as strict:
            assert strict.get("/api/status").json()["supported_formats"] == [
                ".doc",
                ".docx",
                ".hwp",
                ".hwpx",
                ".md",
                ".pdf",
                ".txt",
            ]

    def test_status_counts_documents_waiting_at_root(self, client: TestClient) -> None:
        vault = client.app.state.engine.vault  # type: ignore[attr-defined]
        (Path(vault.root) / "waiting.txt").write_bytes(b"waiting for a useful class")

        body = client.get("/api/status").json()

        assert body["documents"] == 1
        assert body["placed"] == 1
        assert body["inbox"] == 0


class TestOpenVault:
    def test_opens_the_active_vault_root(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[Path] = []
        monkeypatch.setattr(api_app, "_open_in_file_manager", opened.append)

        response = client.post("/api/vault/open")

        root = Path(client.app.state.engine.vault.root)  # type: ignore[attr-defined]
        assert response.status_code == 200
        assert response.json() == {"opened": str(root)}
        assert opened == [root]

    def test_reports_when_the_file_manager_cannot_open(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(_: Path) -> None:
            raise OSError("no file manager")

        monkeypatch.setattr(api_app, "_open_in_file_manager", fail)

        response = client.post("/api/vault/open")

        assert response.status_code == 500
        assert response.json()["detail"] == "볼트 폴더를 파일 탐색기에서 열지 못했습니다."


class TestOpenFile:
    def test_serves_a_documents_bytes(self, client: TestClient) -> None:
        _seed_document(client, body="아폴로 계약서 2023 고유내용")
        r = client.get("/api/file", params={"path": "아폴로/2023/contract.txt"})
        assert r.status_code == 200
        assert "아폴로 계약서 2023 고유내용" in r.content.decode("utf-8")
        assert r.headers["content-disposition"].startswith("attachment;")
        assert r.headers["content-type"].startswith("application/octet-stream")
        assert r.headers["x-content-type-options"] == "nosniff"

    def test_missing_file_is_404(self, client: TestClient) -> None:
        assert client.get("/api/file", params={"path": "아폴로/2023/nope.txt"}).status_code == 404

    def test_a_path_escape_is_refused(self, client: TestClient) -> None:
        # read_bytes -> resolve() rejects anything leaving the vault.
        r = client.get("/api/file", params={"path": "../../../etc/passwd"})
        assert r.status_code == 404


class TestSearch:
    def test_finds_a_document_by_filename_and_returns_its_folder(self, client: TestClient) -> None:
        _seed_document(client, name="project-report.txt")

        body = client.get("/api/search", params={"q": "project-report"}).json()

        assert [item["path"] for item in body] == ["아폴로/2023/project-report.txt"]

    def test_finds_card_summary_and_topics_case_insensitively(self, client: TestClient) -> None:
        _seed_document(client)

        summary = client.get("/api/search", params={"q": "유지보수"}).json()
        topic = client.get("/api/search", params={"q": "아폴로"}).json()

        assert summary[0]["summary"]
        assert topic[0]["topics"]

    def test_blank_search_returns_no_documents(self, client: TestClient) -> None:
        _seed_document(client)

        assert client.get("/api/search", params={"q": "   "}).json() == []

    def test_search_limit_is_bounded(self, client: TestClient) -> None:
        _seed_document(client, name="one.txt", body="아폴로 첫 번째 계약")
        _seed_document(client, name="two.txt", body="아폴로 두 번째 계약")

        body = client.get("/api/search", params={"q": "아폴로", "limit": 1}).json()

        assert len(body) == 1

    def test_search_cache_refreshes_when_a_document_is_added(self, client: TestClient) -> None:
        _seed_document(client, name="first.txt", body="첫 번째 고유 검색어")
        assert len(client.get("/api/search", params={"q": ".txt"}).json()) == 1

        _seed_document(client, name="second.txt", body="두 번째 고유 검색어")

        assert len(client.get("/api/search", params={"q": ".txt"}).json()) == 2


class TestAcceptedUploads:
    """The web flow accepts every document format supported end to end."""

    @pytest.fixture
    def strict(self, settings: Settings, llm: FakeLLM) -> Iterator[TestClient]:
        app = create_app(settings)  # the product's own list, not the fixture's wider one
        engine = build(settings, llm=llm)
        seed_folder(Path(engine.vault.root))
        app.state.engine = engine
        with TestClient(app) as test_client:
            yield test_client

    def test_a_text_file_is_accepted(self, strict: TestClient) -> None:
        r = strict.post(
            "/api/batches",
            files={"files": ("contract.txt", "아폴로 계약서".encode(), "text/plain")},
        )

        assert r.status_code == 202

    def test_the_refusal_names_every_kind_it_turned_away(self, strict: TestClient) -> None:
        r = strict.post(
            "/api/batches",
            files=[
                ("files", ("a.rtf", b"x", "application/octet-stream")),
                ("files", ("b.odt", b"x", "application/octet-stream")),
            ],
        )

        assert r.status_code == 400
        assert ".rtf" in r.json()["detail"] and ".odt" in r.json()["detail"]

    def test_nothing_is_staged_when_a_batch_is_refused(self, strict: TestClient) -> None:
        engine = strict.app.state.engine  # type: ignore[attr-defined]

        strict.post("/api/batches", files={"files": ("a.rtf", b"x", "text/rtf")})

        assert not list((Path(engine.vault.root) / "_inbox").glob("*"))

    @pytest.mark.parametrize(
        ("filename", "data", "label"),
        [
            ("fake.doc", b"not ole", "DOC"),
            ("fake.hwp", b"not ole", "HWP"),
            ("fake.docx", b"not zip", "DOCX"),
            ("fake.hwpx", b"not zip", "HWPX"),
        ],
    )
    def test_a_renamed_office_file_is_refused(
        self, strict: TestClient, filename: str, data: bytes, label: str
    ) -> None:
        response = strict.post(
            "/api/batches", files={"files": (filename, data, "application/octet-stream")}
        )
        assert response.status_code == 400
        assert f"올바른 {label}" in response.json()["detail"]

    def test_a_file_renamed_to_pdf_is_refused(self, strict: TestClient) -> None:
        r = strict.post(
            "/api/batches", files={"files": ("paper.pdf", b"not really a pdf", "application/pdf")}
        )

        assert r.status_code == 400
        assert "올바른 PDF" in r.json()["detail"]

    def test_an_invalid_pdf_batch_stages_nothing(self, strict: TestClient) -> None:
        engine = strict.app.state.engine  # type: ignore[attr-defined]

        response = strict.post(
            "/api/batches",
            files=[
                ("files", ("valid.pdf", b"%PDF-1.7\n", "application/pdf")),
                ("files", ("invalid.pdf", b"plain text", "application/pdf")),
            ],
        )

        assert response.status_code == 400
        assert not list((Path(engine.vault.root) / "_inbox").glob("*"))

    def test_an_oversized_file_is_refused(
        self, strict: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bismuth.api.app.MAX_UPLOAD_BYTES", 8)

        response = strict.post(
            "/api/batches",
            files={"files": ("large.pdf", b"%PDF-1.7\nmore", "application/pdf")},
        )

        assert response.status_code == 413

    def test_too_many_files_are_refused(
        self, strict: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bismuth.api.app.MAX_UPLOAD_FILES", 1)

        response = strict.post(
            "/api/batches",
            files=[
                ("files", ("one.pdf", b"%PDF-1.7\n", "application/pdf")),
                ("files", ("two.pdf", b"%PDF-1.7\n", "application/pdf")),
            ],
        )

        assert response.status_code == 413

    def test_oversized_batch_is_refused_before_staging(
        self, strict: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = strict.app.state.engine  # type: ignore[attr-defined]
        monkeypatch.setattr("bismuth.api.app.MAX_UPLOAD_TOTAL_BYTES", 12)

        response = strict.post(
            "/api/batches",
            files=[
                ("files", ("one.pdf", b"%PDF-1.7\n", "application/pdf")),
                ("files", ("two.pdf", b"%PDF-1.7\n", "application/pdf")),
            ],
        )

        assert response.status_code == 413
        assert not list((Path(engine.vault.root) / "_inbox").glob("*"))


class TestLocalSecurityBoundary:
    def test_rejects_untrusted_hosts(self, client: TestClient) -> None:
        assert client.get("/api/status", headers={"host": "example.test"}).status_code == 400

    def test_rejects_external_write_origins(self, client: TestClient) -> None:
        response = client.post("/api/batches", headers={"origin": "https://example.test"})

        assert response.status_code == 403

    def test_hides_unhandled_exception_details(self, settings: Settings) -> None:
        app = create_app(settings)

        @app.get("/boom")
        def boom() -> None:
            raise RuntimeError("private detail")

        with TestClient(app, raise_server_exceptions=False) as local:
            response = local.get("/boom")

        assert response.status_code == 500
        assert response.json() == {"detail": "Internal server error"}

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
    def test_cli_accepts_loopback_hosts(self, host: str) -> None:
        assert _is_loopback_host(host)

    def test_cli_rejects_external_hosts(self) -> None:
        assert not _is_loopback_host("0.0.0.0")
        assert not _is_loopback_host("192.168.1.10")


class TestAnsweringSide:
    """Chat settings own the answering provider, model, and credentials."""

    def _configured(self, client: TestClient) -> None:
        client.post(
            "/api/setup",
            json={
                "provider_id": "custom",
                "api_base": "http://filing/v1",
                "api_key": "key-filing",
                "model": "filing-model",
                "vault_path": str(client.app.state.engine.vault.root),  # type: ignore[attr-defined]
            },
        )

    def test_a_second_model_on_the_same_server(self, client: TestClient) -> None:
        self._configured(client)

        r = client.post("/api/setup/chat", json={"model": "answering-model"})

        assert r.status_code == 200
        assert r.json()["chat_model"] == "answering-model"
        assert r.json()["chat_provider_id"] == ""
        assert r.json()["chat_is_separate"] is True

    def test_the_same_name_is_not_a_second_model(self, client: TestClient) -> None:
        """Otherwise the two drift apart the next time the filing model is changed."""
        self._configured(client)

        r = client.post("/api/setup/chat", json={"model": "filing-model"})

        assert r.json()["chat_model"] == ""
        assert r.json()["chat_is_separate"] is False

    def test_another_provider_entirely(self, client: TestClient) -> None:
        self._configured(client)

        r = client.post(
            "/api/setup/chat",
            json={"provider_id": "anthropic", "model": "claude-x", "api_key": "key-answering"},
        )

        assert r.status_code == 200
        body = r.json()
        assert body["chat_provider_id"] == "anthropic"
        assert body["chat_api_key_tail"].endswith("ring")
        # and the filing side is exactly as it was
        assert body["provider_id"] == "custom"
        assert body["model"] == "filing-model"
        assert body["api_key_tail"].endswith("ling")

    def test_a_provider_that_needs_a_key_is_refused_without_one(self, client: TestClient) -> None:
        self._configured(client)

        r = client.post("/api/setup/chat", json={"provider_id": "anthropic", "model": "claude-x"})

        assert r.status_code == 400
        assert "키" in r.json()["detail"]

    def test_an_unknown_provider_is_refused(self, client: TestClient) -> None:
        self._configured(client)

        r = client.post("/api/setup/chat", json={"provider_id": "nowhere", "model": "x"})

        assert r.status_code == 400

    def test_the_engine_answers_from_the_new_model_afterwards(self, client: TestClient) -> None:
        """Updating settings also replaces the running engine."""
        self._configured(client)

        client.post("/api/setup/chat", json={"model": "answering-model"})

        settings = client.app.state.settings  # type: ignore[attr-defined]
        assert settings.chat().model.endswith("answering-model")
        assert settings.librarian().model.endswith("filing-model")

    def test_model_runtime_settings_are_saved_and_applied(self, client: TestClient) -> None:
        self._configured(client)

        r = client.post(
            "/api/setup/chat",
            json={
                "provider_id": "openai",
                "model": "gpt-5.6-luna",
                "api_key": "test-key-answering",
                "api_mode": "responses",
                "reasoning_effort": "high",
            },
        )

        assert r.status_code == 200
        assert r.json()["chat_api_mode"] == "responses"
        assert r.json()["chat_reasoning_effort"] == "high"
        settings = client.app.state.settings  # type: ignore[attr-defined]
        endpoint = settings.chat().for_workload(uses_tools=True)
        assert endpoint.model == "openai/responses/gpt-5.6-luna"
        assert endpoint.body["reasoning_effort"] == "high"


class TestChatSpend:
    def test_the_final_stream_event_reports_this_answers_tokens_and_cost(
        self, settings: Settings, llm: FakeLLM
    ) -> None:
        class MeteredModel:
            def __init__(self) -> None:
                self._usage: list[Usage] = []

            async def complete(self, **_: object) -> AssistantMessage:
                usage = Usage(
                    model="test/chat",
                    input_tokens=1200,
                    output_tokens=345,
                    cost_usd=0.0123,
                )
                self._usage.append(usage)
                captured = CURRENT_USAGE.get()
                if captured is not None:
                    captured.append(usage)
                return AssistantMessage(text="근거 있는 답변")

            def drain_usage(self) -> list[Usage]:
                drained, self._usage = self._usage, []
                return drained

        app = create_app(settings)
        app.state.engine = build(settings, llm=llm, chat_model=MeteredModel())
        with TestClient(app) as test_client:
            response = test_client.post("/api/chat", json={"message": "질문"})

        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        answer = next(event for event in events if event["type"] == "answer")
        assert answer["spend"] == {
            "calls": 1,
            "input_tokens": 1200,
            "output_tokens": 345,
            "retries": 0,
            "cost_usd": 0.0123,
            "priced_calls": 1,
        }


class TestFolder:
    def test_folder_returns_its_note_and_contents(self, client: TestClient) -> None:
        _seed_document(client)
        body = client.get("/api/folder", params={"path": "아폴로/2023"}).json()
        assert body["charter"]["purpose"]
        assert body["documents"][0]["title"] == "아폴로 지원 계약서"

    def test_unknown_folder_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/folder", params={"path": "nope"}).status_code == 404

    def test_large_folder_is_returned_in_bounded_pages(self, client: TestClient) -> None:
        vault = Path(client.app.state.engine.vault.root)  # type: ignore[attr-defined]
        folder = vault / "아폴로" / "2023"
        for index in range(205):
            (folder / f"document-{index:03}.txt").write_text(str(index), encoding="utf-8")

        first = client.get("/api/folder", params={"path": "아폴로/2023"}).json()
        second = client.get("/api/folder", params={"path": "아폴로/2023", "offset": 100}).json()
        last = client.get("/api/folder", params={"path": "아폴로/2023", "offset": 200}).json()

        assert first["total"] == 205
        assert first["offset"] == 0
        assert len(first["documents"]) == 100
        assert first["has_more"] is True
        assert first["documents"][-1]["filename"] == "document-099.txt"
        assert second["documents"][0]["filename"] == "document-100.txt"
        assert len(last["documents"]) == 5
        assert last["has_more"] is False

    def test_folder_page_size_is_capped(self, client: TestClient) -> None:
        vault = Path(client.app.state.engine.vault.root)  # type: ignore[attr-defined]
        folder = vault / "아폴로" / "2023"
        for index in range(220):
            (folder / f"document-{index:03}.txt").write_text(str(index), encoding="utf-8")

        body = client.get("/api/folder", params={"path": "아폴로/2023", "limit": 10_000}).json()

        assert body["limit"] == 200
        assert len(body["documents"]) == 200
        assert body["has_more"] is True


class TestBatchUpload:
    def test_duplicate_bytes_in_one_batch_are_filed_once(self, client: TestClient) -> None:
        submitted = client.post(
            "/api/batches",
            files=[
                ("files", ("one.txt", b"same bytes", "text/plain")),
                ("files", ("copy.txt", b"same bytes", "text/plain")),
            ],
        ).json()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            batch = client.get(f"/api/batches/{submitted['id']}").json()
            if batch["status"] == "done":
                break
            time.sleep(0.01)

        engine = client.app.state.engine  # type: ignore[attr-defined]
        assert batch["completed"] == 2
        assert batch["duplicate"] == 1
        assert engine.catalog.card_count() == 1

    def test_one_file_uses_simple_filing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = client.app.state.engine  # type: ignore[attr-defined]
        filed: list[int] = []
        original = engine.simple.file

        async def record(batch: list[tuple[object, object, object]]) -> None:
            filed.append(len(batch))
            await original(batch)  # type: ignore[arg-type]

        monkeypatch.setattr(engine.simple, "file", record)
        submitted = client.post(
            "/api/batches",
            files={"files": ("one.txt", "단일 문서".encode(), "text/plain")},
        ).json()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            batch = client.get(f"/api/batches/{submitted['id']}").json()
            if batch["status"] == "done":
                break
            time.sleep(0.01)

        assert batch["status"] == "done"
        assert filed == [1]

    def test_batch_keeps_processing_after_the_submit_response(self, client: TestClient) -> None:
        submitted = client.post(
            "/api/batches",
            files=[
                ("files", ("one.txt", "아폴로 계약서 하나".encode(), "text/plain")),
                ("files", ("two.txt", "아폴로 계약서 둘".encode(), "text/plain")),
            ],
        )

        assert submitted.status_code == 202
        batch = submitted.json()
        assert batch["total"] == 2
        assert batch["completed"] <= 2

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            batch = client.get(f"/api/batches/{batch['id']}").json()
            if batch["status"] == "done":
                break
            time.sleep(0.01)

        assert batch["status"] == "done"
        assert batch["completed"] == 2
        assert batch["failed"] == 0
        # A newly loaded page discovers the same server-owned batch.
        assert batch["id"] in {item["id"] for item in client.get("/api/batches").json()}

    def test_filing_failures_count_as_completed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = client.app.state.engine  # type: ignore[attr-defined]

        async def fail_filing(*_: object) -> None:
            raise RuntimeError("filing failed")

        monkeypatch.setattr(engine.simple, "file", fail_filing)
        submitted = client.post(
            "/api/batches",
            files=[
                ("files", ("one.txt", b"one", "text/plain")),
                ("files", ("two.txt", b"two", "text/plain")),
            ],
        ).json()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            batch = client.get(f"/api/batches/{submitted['id']}").json()
            if batch["status"] == "done":
                break
            time.sleep(0.01)

        assert batch["completed"] == 2
        assert batch["failed"] == 2


class TestCardReplay:
    def test_empty_tree_keeps_documents_and_sidecars(self, client: TestClient) -> None:
        _seed_document(client)
        vault = client.app.state.engine.vault  # type: ignore[attr-defined]

        response = client.post("/api/tree/empty")

        assert response.status_code == 200
        assert response.json() == {"documents": 1, "folders": 2}
        assert (vault.root / "contract.txt").is_file()
        assert (vault.root / "contract.txt.md").is_file()
        assert not (vault.root / "아폴로").exists()

    def test_refile_all_reuses_the_saved_card(self, client: TestClient) -> None:
        _seed_document(client)
        vault = client.app.state.engine.vault  # type: ignore[attr-defined]
        original_sidecar = (vault.root / "아폴로/2023/contract.txt.md").read_bytes()
        card_files = vault.root / ".bismuth/cards"
        original_catalog = {path.name: path.read_bytes() for path in card_files.glob("*.json")}

        def refile() -> dict[str, object]:
            submitted = client.post("/api/refile-all")
            assert submitted.status_code == 202
            batch = submitted.json()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                batch = client.get(f"/api/batches/{batch['id']}").json()
                if batch["status"] in {"done", "failed"}:
                    return batch
                time.sleep(0.01)
            return batch

        first = refile()
        second = refile()

        assert first["status"] == second["status"] == "done"
        assert first["completed"] == second["completed"] == 1
        assert (vault.root / "contract.txt").is_file()
        assert (vault.root / "contract.txt.md").read_bytes() == original_sidecar
        assert {
            path.name: path.read_bytes() for path in card_files.glob("*.json")
        } == original_catalog
        assert not (vault.root / "_inbox/contract.txt").exists()


class TestUndo:
    def test_undoing_something_unknown_is_a_clean_400(self, client: TestClient) -> None:
        assert client.post("/api/journal/nope/undo").status_code == 400


class TestDelete:
    def _file(self, client: TestClient) -> None:
        _seed_document(client)

    def test_delete_a_file(self, client: TestClient) -> None:
        self._file(client)
        r = client.post("/api/delete", json={"path": "아폴로/2023/contract.txt"})
        assert r.status_code == 200
        vault = client.app.state.engine.vault  # type: ignore[attr-defined]
        assert not (vault.root / "아폴로/2023/contract.txt").exists()

    def test_delete_a_folder(self, client: TestClient) -> None:
        self._file(client)
        r = client.post("/api/delete", json={"path": "아폴로", "is_folder": True})
        assert r.status_code == 200
        assert not (client.app.state.engine.vault.root / "아폴로").exists()  # type: ignore[attr-defined]

    def test_deleting_the_inbox_is_refused(self, client: TestClient) -> None:
        assert (
            client.post("/api/delete", json={"path": "_inbox", "is_folder": True}).status_code
            == 400
        )

    def test_delete_several_folders_at_once(self, client: TestClient) -> None:
        self._file(client)
        r = client.post("/api/delete-folders", json={"paths": ["아폴로/2023", "아폴로"]})

        assert r.status_code == 200
        # Nested selection: the child is covered by the parent, so its document is
        # counted once rather than twice.
        assert r.json() == {"files": 1, "folders": 2}
        assert not (client.app.state.engine.vault.root / "아폴로").exists()  # type: ignore[attr-defined]

    def test_a_bad_path_fails_the_whole_folder_batch(self, client: TestClient) -> None:
        self._file(client)
        r = client.post("/api/delete-folders", json={"paths": ["아폴로", "_inbox"]})

        assert r.status_code == 400
        assert (client.app.state.engine.vault.root / "아폴로").exists()  # type: ignore[attr-defined]

    def test_deleting_several_folders_is_one_undo(self, client: TestClient) -> None:
        self._file(client)
        client.post("/api/delete-folders", json={"paths": ["아폴로"]})
        assert client.get("/api/status").json()["documents"] == 0

        entry = next(e for e in client.get("/api/journal").json() if "delete folder" in e["reason"])
        client.post(f"/api/journal/{entry['id']}/undo")
        assert client.get("/api/status").json()["documents"] == 1

    def test_document_count_follows_disk_after_undo(self, client: TestClient) -> None:
        # Count must reflect files restored on disk, not cache cards.
        self._file(client)
        client.post("/api/delete", json={"path": "아폴로", "is_folder": True})
        assert client.get("/api/status").json()["documents"] == 0

        entry = next(e for e in client.get("/api/journal").json() if "delete folder" in e["reason"])
        client.post(f"/api/journal/{entry['id']}/undo")

        body = client.get("/api/status").json()
        assert body["documents"] == 1
        folder = client.get("/api/folder", params={"path": "아폴로/2023"}).json()
        assert folder["documents"][0]["title"] == "아폴로 지원 계약서"


class TestUi:
    def test_the_page_is_served(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "Bismuth" in response.text


class TestBatchReadsAheadButFilesInOrder:
    def test_documents_are_filed_in_the_order_they_were_submitted(
        self, client: TestClient, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Reading runs ahead; filing must not. The tree a document lands in is the one the
        documents before it built, so a reordered batch is a different archive.

        Filing takes a handful at a time now, so what has to hold is that the handfuls
        arrive in order and each is itself in order."""
        filed: list[str] = []
        engine = client.app.state.engine
        original = engine.simple.file

        async def record(batch, **kwargs):  # type: ignore[no-untyped-def]
            filed.extend(one.source.filename for _, _, one in batch)
            return await original(batch, **kwargs)

        monkeypatch.setattr(engine.simple, "file", record)
        names = [f"doc{index}.txt" for index in range(6)]
        submitted = client.post(
            "/api/batches",
            files=[("files", (name, f"문서 {name} 내용".encode(), "text/plain")) for name in names],
        )

        batch = submitted.json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            batch = client.get(f"/api/batches/{batch['id']}").json()
            if batch["status"] == "done":
                break
            time.sleep(0.05)

        assert batch["status"] == "done"
        assert filed == names


class TestTheWizardDoesNotCarryAnEndpointForward:
    """Provider changes clear endpoint-specific headers and body values."""

    def test_changing_provider_clears_the_previous_headers_and_body(
        self,
        client: TestClient,
        tmp_path: Path,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = tmp_path / "config.json"
        monkeypatch.setattr("bismuth.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("bismuth.config.CONFIG_FILE", config)
        monkeypatch.setitem(Settings.model_config, "json_file", config)
        monkeypatch.setattr(
            "bismuth.api.app.supports_response_schema", lambda **_: True, raising=True
        )

        custom = client.post(
            "/api/setup",
            json={
                "provider_id": "custom",
                "api_base": "http://gateway.internal/v1",
                "api_headers": {"Cookie": "gateway-session"},
                "api_body": {"chat_template_kwargs": {"enable_thinking": False}},
                "model": "qwen3-32b",
                "vault_path": str(vault_path),
            },
        )
        assert custom.status_code == 200
        assert custom.json()["api_headers"] == {"Cookie": "gateway-session"}

        # Hosted providers do not inherit custom endpoint options.
        hosted = client.post(
            "/api/setup",
            json={
                "provider_id": "openai",
                "api_key": "test-key-test",
                "api_base": None,
                "api_headers": {},
                "api_body": {},
                "model": "gpt-4o",
                "vault_path": str(vault_path),
            },
        )

        assert hosted.status_code == 200
        state = hosted.json()
        assert state["provider_id"] == "openai"
        assert state["api_headers"] == {}
        assert state["api_body"] == {}
        assert state["api_base"] is None
        assert state["native_schema"] is None
        assert json.loads(config.read_text(encoding="utf-8"))["api_headers"] == {}
