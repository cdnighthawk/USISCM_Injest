from pathlib import Path
from unittest.mock import MagicMock

from usiscm_ingest.classify import FileCategory
from usiscm_ingest.client import UsiscmClient, _best_project_match
from usiscm_ingest.config import Settings
from usiscm_ingest.package import ingest_source


def test_import_package_dry_run_does_not_post(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "A-101.pdf").write_bytes(b"x")
    manifest = ingest_source(root)
    client = UsiscmClient(
        Settings(base_url="https://www.usiscm.com", ms_access_token="ms-token", token_path=tmp_path / "ms.json")
    )
    client.resolve_project = MagicMock(return_value={"id": "proj-1", "name": "Job", "kind": "job"})  # type: ignore[method-assign]
    client.session.post = MagicMock()

    result = client.import_package(manifest, project_id="proj-1", dry_run=True)

    client.session.post.assert_not_called()
    assert result.project_id == "proj-1"
    assert result.imported == 1
    names = result.details[0]["names"]
    assert names[0]["sheet_number"] == "A-101"


def test_microsoft_drawing_uses_native_b2(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "A1-001_ENTRY.pdf").write_bytes(b"%PDF-1.4 drawing")
    manifest = ingest_source(root)

    posts: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        response = MagicMock()
        response.ok = True
        response.status_code = 201
        response.headers = {}
        if "/jobs/" in url and url.endswith("/drawings"):
            response.json.return_value = {
                "item": {"id": "draw-1"},
                "upload": {
                    "mode": "b2_native",
                    "url": "https://pod-000.backblaze.com/b2api/v2/b2_upload_file/x",
                    "authorization": "b2tok",
                    "file_name": "drawings/A1-001.pdf",
                },
            }
        elif url.endswith("/ack-file"):
            response.status_code = 200
            response.json.return_value = {"item": {"id": "draw-1", "file_pending": False}}
        else:
            response.status_code = 200
            response.json.return_value = {"item": {}}
        return response

    b2_calls: list[dict] = []

    def fake_b2(hint, payload, **kwargs):
        b2_calls.append({"hint": hint, "payload": payload, "headers_auth": hint.get("authorization")})
        return {"fileId": "fid", "fileName": hint["file_name"], "contentSha1": "abc"}

    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ms_access_token="ms-token",
            token_path=tmp_path / "ms.json",
            sheet_ai=False,
        ),
        sleeper=lambda _: None,
        b2_post=fake_b2,
    )
    client.token = "ms-token"
    client.auth_mode = "microsoft"
    client.session.headers["Authorization"] = "Bearer ms-token"
    client.resolve_project = MagicMock(  # type: ignore[method-assign]
        return_value={"id": "job-uuid", "name": "Job", "kind": "job", "job_id": "job-uuid"}
    )
    client.session.post = MagicMock(side_effect=fake_post)

    result = client.import_package(manifest, project_id="job-uuid")
    urls = [url for url, _ in posts]
    assert any("/api/v1/jobs/job-uuid/drawings" in url for url in urls)
    assert any(url.endswith("/ack-file") for url in urls)
    assert b2_calls and b2_calls[0]["payload"].startswith(b"%PDF")
    assert b2_calls[0]["hint"]["authorization"] == "b2tok"
    assert result.imported == 1
    assert result.errors == []
    create_body = posts[0][1]["json"]["item"]
    assert create_body["sheetNumber"] == "A1-001"
    assert "ENTRY" in (create_body["sheetTitle"] or "")


def test_s3_mint_is_refused_and_logged(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "A-101.pdf").write_bytes(b"pdf")
    manifest = ingest_source(root)
    issues: list[dict] = []

    def fake_post(url, **kwargs):
        response = MagicMock()
        response.ok = True
        response.headers = {}
        if "/jobs/" in url:
            response.status_code = 201
            response.json.return_value = {
                "item": {"id": "draw-1"},
                "upload": {
                    "mode": "s3_presigned_put",
                    "url": "https://s3.us-west-004.backblazeb2.com/bucket/key?X-Amz-Credential=x",
                    "authorization": "nope",
                },
            }
        elif url.endswith("/ingest/errors"):
            issues.append(kwargs.get("json") or {})
            response.status_code = 201
            response.json.return_value = {"item": {"id": "err-1"}}
        else:
            response.status_code = 200
            response.json.return_value = {}
        return response

    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ms_access_token="ms-token",
            token_path=tmp_path / "ms.json",
            sheet_ai=False,
        ),
        sleeper=lambda _: None,
        b2_post=lambda *a, **k: (_ for _ in ()).throw(AssertionError("B2 must not be called")),
    )
    client.token = "ms-token"
    client.auth_mode = "microsoft"
    client.resolve_project = MagicMock(return_value={"id": "job-uuid", "kind": "job"})  # type: ignore[method-assign]
    client.session.post = MagicMock(side_effect=fake_post)

    result = client.import_package(manifest, project_id="job-uuid")
    assert result.imported == 0
    assert result.errors
    assert any("S3" in (err["error"] or "") or "s3" in (err["error"] or "").lower() for err in result.errors)
    assert issues and issues[0]["kind"] == "drawing"


def test_ambiguous_name_still_uploads_and_reports_issue(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "Drawings" / "scan_001.pdf").parent.mkdir(parents=True)
    (root / "Drawings" / "scan_001.pdf").write_bytes(b"%PDF scan")
    manifest = ingest_source(root)
    issues: list[dict] = []

    def fake_post(url, **kwargs):
        response = MagicMock()
        response.ok = True
        response.headers = {}
        if url.endswith("/ingest/errors"):
            issues.append(kwargs.get("json") or {})
            response.status_code = 201
            response.json.return_value = {"item": {"id": "err-name"}}
        elif "/jobs/" in url:
            response.status_code = 201
            response.json.return_value = {
                "item": {"id": "draw-scan"},
                "upload": {
                    "mode": "b2_native",
                    "url": "https://pod.backblaze.com/b2api/v2/b2_upload_file/x",
                    "authorization": "tok",
                    "file_name": "scan.pdf",
                },
            }
        else:
            response.status_code = 200
            response.json.return_value = {"item": {}}
        return response

    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ms_access_token="ms-token",
            token_path=tmp_path / "ms.json",
            sheet_ai=False,
        ),
        sleeper=lambda _: None,
        b2_post=lambda hint, payload, **k: {"fileId": "1", "fileName": "scan.pdf"},
    )
    client.token = "ms-token"
    client.auth_mode = "microsoft"
    client.resolve_project = MagicMock(return_value={"id": "job-uuid", "kind": "job"})  # type: ignore[method-assign]
    client.session.post = MagicMock(side_effect=fake_post)

    result = client.import_package(manifest, project_id="job-uuid")
    assert result.imported == 1
    assert issues
    assert issues[0]["kind"] == "naming"
    assert issues[0]["source"] == "usiscm_ingest"
    assert issues[0]["filename"] == "scan_001.pdf"


def test_project_match_uses_v1_name_field() -> None:
    projects = [
        {"id": "1", "name": "Kaiser Permanente San Rafael MOB", "number": "KP-SR-01"},
        {"id": "2", "name": "Sutter Oakland", "number": "SO-9"},
    ]
    match = _best_project_match("Kaiser Permanente San Rafael", projects)
    assert match is not None
    assert match["id"] == "1"


def _native_upload(file_name: str) -> dict:
    return {
        "mode": "b2_native",
        "url": "https://pod-000.backblaze.com/b2api/v2/b2_upload_file/x",
        "authorization": "b2tok",
        "file_name": file_name,
    }


def test_microsoft_document_uses_native_b2_even_with_ingest_key(tmp_path: Path) -> None:
    """WIN-C7 had a Microsoft session and an ingest key. Specs must not multipart POST /api/documents."""
    root = tmp_path / "Job"
    root.mkdir()
    (root / "A-101.pdf").write_bytes(b"%PDF-1.4 drawing")
    (root / "Project Manual.pdf").write_bytes(b"spec-bytes")
    manifest = ingest_source(root)
    assert FileCategory.SPEC in {item.category for item in manifest.files}

    b2_calls: list[dict] = []

    def fake_b2(hint, payload, **kwargs):
        b2_calls.append({"payload": payload, "content_type": kwargs.get("content_type"), "auth": hint.get("authorization")})
        return {"fileId": "fid", "fileName": hint.get("file_name"), "contentSha1": "abc"}

    def fake_post(url, **kwargs):
        assert "files" not in kwargs
        response = MagicMock()
        response.ok = True
        response.headers = {}
        response.status_code = 201
        if "/jobs/" in url and url.endswith("/drawings"):
            response.json.return_value = {
                "item": {"id": "draw-1"},
                "upload": _native_upload("drawings/A-101.pdf"),
            }
        elif "/jobs/" in url and url.endswith("/documents"):
            body = kwargs.get("json") or {}
            item = body.get("item") or {}
            assert item.get("documentType") == "specification"
            assert item.get("sourceFileName") == "Project Manual.pdf"
            assert item.get("mimeType") == "application/pdf"
            response.json.return_value = {
                "item": {"id": "doc-9", "file_pending": True},
                "upload": _native_upload("documents/Project-Manual.pdf"),
            }
        elif url.endswith("/ack-file"):
            response.status_code = 200
            ack = (kwargs.get("json") or {}).get("item") or {}
            assert ack.get("contentType") == "application/pdf"
            assert "spec-bytes" not in str(kwargs.get("json"))
            assert "%PDF" not in str(kwargs.get("json"))
            response.json.return_value = {"item": {"file_pending": False}}
        else:
            response.status_code = 200
            response.json.return_value = {"item": {}}
        return response

    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ms_access_token="ms-token",
            ingest_api_key="night-key",
            token_path=tmp_path / "ms.json",
            sheet_ai=False,
        ),
        sleeper=lambda _: None,
        b2_post=fake_b2,
    )
    client.token = "ms-token"
    client.auth_mode = "microsoft"
    client.resolve_project = MagicMock(  # type: ignore[method-assign]
        return_value={"id": "job-uuid", "name": "Job", "kind": "job", "job_id": "job-uuid"}
    )
    client.session.post = MagicMock(side_effect=fake_post)

    result = client.import_package(manifest, project_id="job-uuid")
    urls = [call.args[0] for call in client.session.post.call_args_list]
    assert any(url.endswith("/api/v1/jobs/job-uuid/documents") for url in urls)
    assert any(url.endswith("/api/v1/documents/doc-9/ack-file") for url in urls)
    assert not any(url.rstrip("/").endswith("/api/documents") for url in urls)
    assert not any("/api/v1/ingest/files" in url for url in urls)
    assert b"spec-bytes" in [call["payload"] for call in b2_calls]
    assert all(call["auth"] == "b2tok" for call in b2_calls)
    assert result.imported == 2
    assert result.errors == []
    assert result.details[-1]["endpoint"] == "b2-native"
    assert result.details[-1]["document_id"] == "doc-9"


def test_document_s3_mint_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "Project Manual.pdf").write_bytes(b"spec")
    manifest = ingest_source(root)
    issues: list[dict] = []

    def fake_post(url, **kwargs):
        response = MagicMock()
        response.ok = True
        response.headers = {}
        if "/jobs/" in url and url.endswith("/documents"):
            response.status_code = 201
            response.json.return_value = {
                "item": {"id": "doc-1"},
                "upload": {
                    "mode": "s3_presigned_put",
                    "url": "https://s3.us-west-004.backblazeb2.com/bucket/key?X-Amz-Credential=x",
                    "authorization": "nope",
                },
            }
        elif url.endswith("/ingest/errors"):
            issues.append(kwargs.get("json") or {})
            response.status_code = 201
            response.json.return_value = {"item": {"id": "err-1"}}
        else:
            response.status_code = 200
            response.json.return_value = {}
        return response

    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ms_access_token="ms-token",
            token_path=tmp_path / "ms.json",
            sheet_ai=False,
        ),
        sleeper=lambda _: None,
        b2_post=lambda *a, **k: (_ for _ in ()).throw(AssertionError("B2 must not be called")),
    )
    client.token = "ms-token"
    client.auth_mode = "microsoft"
    client.resolve_project = MagicMock(return_value={"id": "job-uuid", "kind": "job"})  # type: ignore[method-assign]
    client.session.post = MagicMock(side_effect=fake_post)

    result = client.import_package(manifest, project_id="job-uuid")
    assert result.imported == 0
    assert result.errors
    assert any("s3" in (err["error"] or "").lower() for err in result.errors)
    assert issues and issues[0]["kind"] == "document"


def test_ingest_key_document_duplicate_skips_b2(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "Project Manual.pdf").write_bytes(b"spec")
    manifest = ingest_source(root)

    def fake_post(url, **kwargs):
        assert "files" not in kwargs
        response = MagicMock()
        response.ok = True
        response.status_code = 200
        response.headers = {}
        response.json.return_value = {
            "document": {"id": "doc-kept", "file_pending": False},
            "duplicate": True,
        }
        return response

    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ingest_api_key="night-key",
            token_path=tmp_path / "ms.json",
            sheet_ai=False,
        ),
        b2_post=lambda *a, **k: (_ for _ in ()).throw(AssertionError("B2 must not be called")),
    )
    client.token = "night-key"
    client.auth_mode = "ingest_key"
    client.resolve_project = MagicMock(return_value={"id": "job-1", "name": "Job"})  # type: ignore[method-assign]
    client.session.post = MagicMock(side_effect=fake_post)

    result = client.import_package(manifest, project_id="job-1")
    urls = [call.args[0] for call in client.session.post.call_args_list]
    assert urls == ["https://www.usiscm.com/api/documents"]
    assert result.imported == 1
    assert result.errors == []
    assert result.details[0]["document_id"] == "doc-kept"


def test_content_type_for_office_files() -> None:
    from usiscm_ingest.client import content_type_for

    assert content_type_for(".pdf") == "application/pdf"
    assert content_type_for(".docx").endswith("wordprocessingml.document")
    assert content_type_for(".xlsx").endswith("spreadsheetml.sheet")
    assert content_type_for(".dwg") == "application/acad"
