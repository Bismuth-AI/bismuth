"""HTTP API endpoint tests, exercised through a real TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestStatus:
    def test_status_answers(self, client: TestClient) -> None:
        assert client.get("/api/status").status_code == 200

    def test_status_reports_the_shape_of_the_vault(self, client: TestClient) -> None:
        body = client.get("/api/status").json()
        assert body["documents"] == 0
        # 아폴로 and 아폴로/2023 are seeded so placement has somewhere to choose.
        assert body["folders"] == 2
        assert "runs_locally" in body


class TestOpenFile:
    def test_serves_a_documents_bytes(self, client: TestClient) -> None:
        client.post(
            "/api/documents",
            files={"files": ("contract.txt", "아폴로 계약서 2023 고유내용".encode(), "text/plain")},
        )
        r = client.get("/api/file", params={"path": "아폴로/2023/contract.txt"})
        assert r.status_code == 200
        assert "아폴로 계약서 2023 고유내용" in r.content.decode("utf-8")

    def test_missing_file_is_404(self, client: TestClient) -> None:
        assert client.get("/api/file", params={"path": "아폴로/2023/nope.txt"}).status_code == 404

    def test_a_path_escape_is_refused(self, client: TestClient) -> None:
        # read_bytes -> resolve() rejects anything leaving the vault.
        r = client.get("/api/file", params={"path": "../../../etc/passwd"})
        assert r.status_code == 404


class TestUpload:
    def test_upload_files_a_document(self, client: TestClient) -> None:
        response = client.post(
            "/api/documents",
            files={"files": ("contract.txt", "아폴로 계약서 2023".encode(), "text/plain")},
        )
        assert response.status_code == 200, response.text
        result = response.json()[0]
        assert result["ok"] is True
        assert result["placed"] is True
        assert result["destination"] == "아폴로/2023"
        assert result["created_folder"] is False  # it was already there

    def test_upload_shows_up_in_the_tree(self, client: TestClient) -> None:
        client.post(
            "/api/documents",
            files={"files": ("contract.txt", "아폴로 계약서 2023".encode(), "text/plain")},
        )
        paths = [f["path"] for f in client.get("/api/tree").json()]
        assert "아폴로/2023" in paths

    def test_re_uploading_the_same_bytes_is_a_no_op(self, client: TestClient) -> None:
        payload = {"files": ("a.txt", b"same bytes", "text/plain")}
        client.post("/api/documents", files=payload)
        second = client.post(
            "/api/documents", files={"files": ("renamed.txt", b"same bytes", "text/plain")}
        )
        assert second.json()[0]["duplicate"] is True

    def test_a_filename_cannot_escape_the_vault(self, client: TestClient) -> None:
        client.post(
            "/api/documents",
            files={"files": ("../../evil.txt", "아폴로 계약서".encode(), "text/plain")},
        )
        root = client.app.state.engine.vault.root  # type: ignore[attr-defined]
        assert not (root.parent.parent / "evil.txt").exists()


class TestFolder:
    def test_folder_returns_its_note_and_contents(self, client: TestClient) -> None:
        client.post(
            "/api/documents",
            files={"files": ("contract.txt", "아폴로 계약서 2023".encode(), "text/plain")},
        )
        body = client.get("/api/folder", params={"path": "아폴로/2023"}).json()
        assert body["charter"]["purpose"]
        assert body["documents"][0]["title"] == "아폴로 지원 계약서"

    def test_unknown_folder_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/folder", params={"path": "nope"}).status_code == 404


class TestUndo:
    def test_a_filed_document_can_be_put_back(self, client: TestClient) -> None:
        client.post(
            "/api/documents",
            files={"files": ("contract.txt", "아폴로 계약서 2023".encode(), "text/plain")},
        )
        vault = client.app.state.engine.vault  # type: ignore[attr-defined]
        assert (vault.root / "아폴로/2023/contract.txt").is_file()

        entry = next(
            e for e in client.get("/api/journal").json() if e["reason"].startswith("file ")
        )
        assert client.post(f"/api/journal/{entry['id']}/undo").status_code == 200
        assert (vault.root / "_inbox/contract.txt").is_file()

    def test_undoing_something_unknown_is_a_clean_400(self, client: TestClient) -> None:
        assert client.post("/api/journal/nope/undo").status_code == 400


class TestDelete:
    def _file(self, client: TestClient) -> None:
        client.post(
            "/api/documents",
            files={"files": ("contract.txt", "아폴로 계약서 2023".encode(), "text/plain")},
        )

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


class TestDuplicate:
    def test_a_duplicate_upload_reports_the_existing_location(self, client: TestClient) -> None:
        payload = {"files": ("a.txt", "같은 내용 아폴로".encode(), "text/plain")}
        client.post("/api/documents", files=payload)
        second = client.post(
            "/api/documents", files={"files": ("b.txt", "같은 내용 아폴로".encode(), "text/plain")}
        )
        result = second.json()[0]
        assert result["duplicate"] is True
        assert result["destination"] == "아폴로/2023"


class TestUi:
    def test_the_page_is_served(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "Bismuth" in response.text
