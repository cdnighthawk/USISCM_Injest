import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from usiscm_ingest.classify import ClassifiedFile, FileCategory
from usiscm_ingest.client import UnattendedAuthError, UsiscmClient, _best_project_match
from usiscm_ingest.config import Settings
from usiscm_ingest.drawing_namer import DrawingName
from usiscm_ingest.microsoft import MicrosoftTokens, load_tokens, save_tokens
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


def _write_pdf(path: Path, pages: list[tuple[str, str, str]]) -> None:
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    try:
        for kind, number, title in pages:
            page = doc.new_page(width=612, height=792)
            if kind == "drawing":
                page.insert_text((72, 72), title)
                page.insert_text((72, 96), f"SHEET NO. {number}")
                page.insert_text((72, 120), f"SHEET TITLE: {title}")
            elif kind == "addendum":
                page.insert_text((72, 72), f"ADDENDUM NO. {number}")
                page.insert_text((72, 96), "This addendum modifies the bid.")
            else:
                page.insert_text((72, 72), title)
        doc.save(path)
    finally:
        doc.close()


def _pdf_pages(payload: bytes) -> int:
    import pymupdf

    try:
        doc = pymupdf.open(stream=payload, filetype="pdf")
    except Exception:
        return 0
    try:
        return int(doc.page_count)
    finally:
        doc.close()


def test_multipage_drawing_is_split_before_native_b2(tmp_path: Path) -> None:
    job = tmp_path / "Job"
    _write_pdf(
        job / "Drawings" / "Architectural.pdf",
        [
            ("drawing", "A-101", "FLOOR PLAN"),
            ("drawing", "S-201", "FOUNDATION PLAN"),
            ("addendum", "4", ""),
        ],
    )
    _write_pdf(
        job / "Combined Bid Set.pdf",
        [("plain", "", "BID FORM"), ("plain", "", "INSTRUCTIONS TO BIDDERS")],
    )
    (job / "Addendum No.4.pdf").write_bytes(b"%PDF-addendum")
    (job / "W9.pdf").write_bytes(b"%PDF-w9")
    manifest = ingest_source(job)
    assert FileCategory.DRAWING in {item.category for item in manifest.files}
    assert all(item.category != FileCategory.DRAWING for item in manifest.files if item.path.name != "Architectural.pdf")

    drawing_bodies: list[dict] = []
    document_names: list[str] = []
    b2_payloads: list[bytes] = []
    ids = {"n": 0}

    def fake_b2(hint, payload, **kwargs):
        b2_payloads.append(payload)
        return {"fileId": "fid", "fileName": hint.get("file_name"), "contentSha1": "abc"}

    def fake_post(url, **kwargs):
        assert "files" not in kwargs
        response = MagicMock()
        response.ok = True
        response.headers = {}
        response.status_code = 201
        ids["n"] += 1
        if "/jobs/" in url and url.endswith("/drawings"):
            item = (kwargs.get("json") or {}).get("item") or {}
            drawing_bodies.append(item)
            response.json.return_value = {
                "item": {"id": f"draw-{ids['n']}"},
                "upload": _native_upload(f"drawings/{item.get('sourceFileName')}"),
            }
        elif "/jobs/" in url and url.endswith("/documents"):
            item = (kwargs.get("json") or {}).get("item") or {}
            document_names.append(item.get("sourceFileName") or "")
            response.json.return_value = {
                "item": {"id": f"doc-{ids['n']}"},
                "upload": _native_upload(f"documents/{item.get('sourceFileName')}"),
            }
        elif url.endswith("/ack-file"):
            response.status_code = 200
            response.json.return_value = {"item": {"file_pending": False}}
        else:
            response.status_code = 200
            response.json.return_value = {"item": {}}
        return response

    estimate = tmp_path / "26092"
    estimate.mkdir()
    missing = tmp_path / "Estimates" / "not-created"
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
    client.resolve_project = MagicMock(  # type: ignore[method-assign]
        return_value={"id": "job-uuid", "name": "Job", "kind": "job", "job_id": "job-uuid"}
    )
    client.session.post = MagicMock(side_effect=fake_post)

    result = client.import_package(manifest, project_id="job-uuid", estimate_folder=str(estimate))
    assert result.errors == []
    assert [body["sheetNumber"] for body in drawing_bodies] == ["A-101", "S-201"]
    assert all(body["sourceFileName"] != "Architectural.pdf" for body in drawing_bodies)
    assert [_pdf_pages(payload) for payload in b2_payloads[:2]] == [1, 1]
    assert any(_pdf_pages(payload) == 2 for payload in b2_payloads)
    assert "Architectural.pdf" not in document_names
    assert "Addendum No.4.pdf" in document_names
    assert "W9.pdf" in document_names
    assert "Combined Bid Set.pdf" in document_names
    combined = next(payload for payload in b2_payloads if _pdf_pages(payload) == 2)
    assert combined
    urls = [call.args[0] for call in client.session.post.call_args_list]
    assert any(url.endswith("/api/v1/jobs/job-uuid/drawings") for url in urls)
    assert any(url.endswith("/documents") for url in urls)
    assert not any(url.rstrip("/").endswith("/api/drawings") for url in urls)
    copied = list((estimate / "02_Processed" / "drawings").glob("*.pdf"))
    assert {path.name for path in copied} == {body["sourceFileName"] for body in drawing_bodies}
    assert not missing.exists()
    assert result.imported == 2 + len(document_names)


def test_ingest_key_uploads_split_sheets_with_split_pages_false(tmp_path: Path) -> None:
    job = tmp_path / "Job"
    _write_pdf(
        job / "Drawings" / "Architectural.pdf",
        [("drawing", "A-101", "FLOOR PLAN"), ("drawing", "S-201", "FOUNDATION PLAN")],
    )
    manifest = ingest_source(job)
    bodies: list[dict] = []
    payloads: list[bytes] = []

    def fake_post(url, **kwargs):
        assert "files" not in kwargs
        response = MagicMock()
        response.ok = True
        response.headers = {}
        response.status_code = 201
        if url.rstrip("/").endswith("/api/drawings"):
            meta = kwargs.get("json") or {}
            bodies.append(meta)
            assert meta["split_pages"] is False
            response.json.return_value = {
                "drawing": {"id": f"draw-{len(bodies)}"},
                "upload": _native_upload(f"drawings/{meta.get('filename')}"),
            }
        elif url.endswith("/ack-file"):
            response.status_code = 200
            response.json.return_value = {"item": {"file_pending": False}}
        else:
            response.status_code = 200
            response.json.return_value = {"item": {}}
        return response

    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ingest_api_key="night-key",
            token_path=tmp_path / "ms.json",
            sheet_ai=False,
        ),
        sleeper=lambda _: None,
        b2_post=lambda hint, payload, **k: payloads.append(payload) or {"fileId": "fid", "fileName": hint.get("file_name")},
    )
    client.token = "night-key"
    client.auth_mode = "ingest_key"
    client.resolve_project = MagicMock(return_value={"id": "job-1", "name": "Job"})  # type: ignore[method-assign]
    client.session.post = MagicMock(side_effect=fake_post)

    result = client.import_package(manifest, project_id="job-1")
    assert result.errors == []
    assert result.imported == 2
    assert [body["sheet_number"] for body in bodies] == ["A-101", "S-201"]
    assert all(body["split_pages"] is False for body in bodies)
    assert [ _pdf_pages(payload) for payload in payloads ] == [1, 1]
    assert all(body["filename"] != "Architectural.pdf" for body in bodies)


def test_content_type_for_office_files() -> None:
    from usiscm_ingest.client import content_type_for

    assert content_type_for(".pdf") == "application/pdf"
    assert content_type_for(".docx").endswith("wordprocessingml.document")
    assert content_type_for(".xlsx").endswith("spreadsheetml.sheet")
    assert content_type_for(".dwg") == "application/acad"


def _json_response(status: int, payload: dict, url: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.reason = "Unauthorized" if status == 401 else "OK"
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    response.encoding = "utf-8"
    return response


class _AuthAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, handler) -> None:
        super().__init__()
        self._handler = handler

    def send(self, request, **kwargs):
        return self._handler(request)


def _microsoft_client(tmp_path: Path, *, expires_in: float = 7200) -> UsiscmClient:
    token_path = tmp_path / "ms.json"
    save_tokens(
        token_path,
        MicrosoftTokens(
            access_token="old-token",
            refresh_token="rtok",
            expires_at=time.time() + expires_in,
            tenant_id="tenant",
            client_id="client",
        ),
    )
    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            token_path=token_path,
            ms_tenant_id="tenant",
            ms_client_id="client",
            sheet_ai=False,
        )
    )
    client.login()
    return client


def _refresh_response() -> MagicMock:
    response = MagicMock()
    response.ok = True
    response.status_code = 200
    response.json.return_value = {"access_token": "new-token", "refresh_token": "rtok", "expires_in": 3600}
    return response


def test_drawings_post_401_refreshes_and_retries_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("USISCM_INGEST_API_KEY", raising=False)
    client = _microsoft_client(tmp_path)
    assert client.session.headers["Authorization"] == "Bearer old-token"
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))
        if len(seen) == 1:
            return _json_response(401, {"error": "authentication required"}, request.url)
        return _json_response(201, {"item": {"id": "draw-1"}}, request.url)

    client.session.mount("https://", _AuthAdapter(handler))
    with patch("usiscm_ingest.microsoft.requests.post", return_value=_refresh_response()) as posted:
        response = client.session.post(
            client._url("api/v1/jobs/job-1/drawings"),
            json={"item": {"sheetNumber": "M-101"}},
            timeout=5,
        )

    assert response.status_code == 201
    assert seen == ["Bearer old-token", "Bearer new-token"]
    assert posted.call_count == 1
    assert posted.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    assert posted.call_args.kwargs["data"]["refresh_token"] == "rtok"
    assert client.session.headers["Authorization"] == "Bearer new-token"
    saved = load_tokens(tmp_path / "ms.json")
    assert saved is not None
    assert saved.access_token == "new-token"


def test_401_after_refresh_is_not_retried_again(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("USISCM_INGEST_API_KEY", raising=False)
    client = _microsoft_client(tmp_path)
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("Authorization") or "")
        return _json_response(401, {"error": "authentication required"}, request.url)

    client.session.mount("https://", _AuthAdapter(handler))
    with patch("usiscm_ingest.microsoft.requests.post", return_value=_refresh_response()) as posted:
        response = client.session.post(client._url("api/v1/ingest/errors"), json={"message": "x"}, timeout=5)

    assert response.status_code == 401
    assert seen == ["Bearer old-token", "Bearer new-token"]
    assert posted.call_count == 1


def test_proactive_refresh_before_request_when_token_is_near_expiry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("USISCM_INGEST_API_KEY", raising=False)
    client = _microsoft_client(tmp_path)
    assert client._microsoft_tokens is not None
    client._microsoft_tokens.expires_at = time.time() + 30
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("Authorization") or "")
        return _json_response(200, {"authenticated": True, "user": {"email": "c@usis.com"}}, request.url)

    client.session.mount("https://", _AuthAdapter(handler))
    with patch("usiscm_ingest.microsoft.requests.post", return_value=_refresh_response()) as posted:
        response = client.session.get(client._url("api/v1/auth/status"), timeout=5)

    assert response.status_code == 200
    assert seen == ["Bearer new-token"]
    assert posted.call_count == 1
    assert client.token == "new-token"


def test_ingest_key_401_does_not_refresh_microsoft(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("USISCM_INGEST_API_KEY", raising=False)
    client = UsiscmClient(
        Settings(base_url="https://www.usiscm.com", ingest_api_key="night-key", token_path=tmp_path / "ms.json")
    )
    client.token = "night-key"
    client.auth_mode = "ingest_key"
    client.session.headers["Authorization"] = "Bearer night-key"
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("Authorization") or "")
        return _json_response(401, {"error": "authentication required"}, request.url)

    client.session.mount("https://", _AuthAdapter(handler))
    with patch("usiscm_ingest.microsoft.requests.post") as posted:
        response = client.session.post(client._url("api/drawings"), json={"filename": "A-101.pdf"}, timeout=5)

    assert response.status_code == 401
    assert seen == ["Bearer night-key"]
    posted.assert_not_called()


def test_ingest_key_401_retries_when_the_env_key_changed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("USISCM_INGEST_API_KEY", "night-key-2")
    client = UsiscmClient(
        Settings(base_url="https://www.usiscm.com", ingest_api_key="night-key", token_path=tmp_path / "ms.json")
    )
    client.token = "night-key"
    client.auth_mode = "ingest_key"
    client.session.headers["Authorization"] = "Bearer night-key"
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("Authorization") or "")
        if len(seen) == 1:
            return _json_response(401, {"error": "authentication required"}, request.url)
        return _json_response(201, {"drawing": {"id": "draw-1"}}, request.url)

    client.session.mount("https://", _AuthAdapter(handler))
    response = client.session.post(client._url("api/drawings"), json={"filename": "A-101.pdf"}, timeout=5)

    assert response.status_code == 201
    assert seen == ["Bearer night-key", "Bearer night-key-2"]
    assert client.token == "night-key-2"


def _b2_upload_hint(file_name: str) -> dict:
    return {
        "mode": "b2_native",
        "url": "https://pod.backblaze.com/b2api/v2/b2_upload_file/x",
        "authorization": "b2tok",
        "file_name": file_name,
    }


def test_expired_access_token_renews_without_device_login(tmp_path: Path, monkeypatch) -> None:
    """Expired access token + valid refresh: catalog, mint, ack, and documents succeed."""
    monkeypatch.delenv("USISCM_INGEST_API_KEY", raising=False)
    token_path = tmp_path / "ms.json"
    save_tokens(
        token_path,
        MicrosoftTokens(
            access_token="expired-access",
            refresh_token="rtok",
            expires_at=time.time() - 120,
            tenant_id="tenant",
            client_id="client",
        ),
    )
    drawing = tmp_path / "M-101.pdf"
    document = tmp_path / "Project Manual.pdf"
    drawing.write_bytes(b"%PDF-1.4 drawing")
    document.write_bytes(b"%PDF-1.4 spec")
    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            token_path=token_path,
            ms_tenant_id="tenant",
            ms_client_id="client",
            sheet_ai=False,
        ),
        b2_post=lambda hint, payload, **kwargs: {
            "fileId": "fid",
            "fileName": hint.get("file_name"),
            "contentSha1": "abc",
        },
    )
    hits: dict[str, int] = {}
    auths: list[str] = []

    def handler(request):
        url = request.url
        hits[url] = hits.get(url, 0) + 1
        auths.append(request.headers.get("Authorization") or "")
        if hits[url] == 1:
            return _json_response(401, {"error": "authentication required"}, url)
        if url.endswith("/api/v1/jobs/job-1/drawings"):
            return _json_response(201, {"item": {"id": "draw-1"}}, url)
        if url.endswith("/api/v1/drawings/draw-1/upload-session"):
            return _json_response(200, _b2_upload_hint("drawings/M-101.pdf"), url)
        if url.endswith("/api/v1/drawings/draw-1/ack-file"):
            return _json_response(200, {"item": {"id": "draw-1", "file_pending": False}}, url)
        if url.endswith("/api/v1/jobs/job-1/documents"):
            return _json_response(201, {"item": {"id": "doc-1"}}, url)
        if url.endswith("/api/v1/documents/doc-1/upload-session"):
            return _json_response(200, _b2_upload_hint("documents/Project-Manual.pdf"), url)
        if url.endswith("/api/v1/documents/doc-1/ack-file"):
            return _json_response(200, {"item": {"id": "doc-1", "file_pending": False}}, url)
        return _json_response(500, {"error": url}, url)

    token_grants: list[str] = []

    def fake_token(url, data=None, **kwargs):
        assert "oauth2/v2.0/token" in url
        body = data or {}
        token_grants.append(str(body.get("grant_type")))
        assert body.get("grant_type") == "refresh_token"
        assert body.get("refresh_token") == "rtok"
        assert "device_code" not in body
        response = MagicMock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {
            "access_token": f"renewed-{len(token_grants)}",
            "refresh_token": "rtok",
            "expires_in": 3600,
        }
        return response

    named = DrawingName(
        sheet_number="M-101",
        sheet_title="HVAC PLAN",
        discipline="Mechanical",
        drawing_set="HVAC",
        revision="",
        confidence=1,
        needs_review=False,
        label_status="named",
    )
    project = {"id": "job-1", "kind": "job", "job_id": "job-1", "name": "Palisades"}
    with patch("usiscm_ingest.microsoft.device_code_login", side_effect=AssertionError("device login")) as device:
        with patch("usiscm_ingest.microsoft.requests.post", side_effect=fake_token):
            client.login()
            assert client.session.headers["Authorization"] == "Bearer renewed-1"
            client.session.mount("https://", _AuthAdapter(handler))
            drawing_result = client._upload_drawing_native_b2(
                project,
                ClassifiedFile(path=drawing, relative_path=drawing.name, category=FileCategory.DRAWING, confidence=1),
                named,
                "HVAC",
            )
            document_result = client._upload_document_native_b2(
                project,
                ClassifiedFile(
                    path=document,
                    relative_path=document.name,
                    category=FileCategory.SPEC,
                    confidence=1,
                ),
                FileCategory.SPEC,
                "HVAC",
            )

    device.assert_not_called()
    assert token_grants
    assert set(token_grants) == {"refresh_token"}
    assert drawing_result["imported"] == 1
    assert document_result["imported"] == 1
    assert all(count == 2 for count in hits.values())
    assert hits
    assert all(auth.startswith("Bearer renewed-") for auth in auths)
    assert "Bearer expired-access" not in auths
    saved = load_tokens(token_path)
    assert saved is not None
    assert saved.access_token != "expired-access"
    assert saved.refresh_token == "rtok"


def test_missing_refresh_token_exits_without_device_login(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("USISCM_INGEST_API_KEY", raising=False)
    token_path = tmp_path / "ms.json"
    save_tokens(
        token_path,
        MicrosoftTokens(
            access_token="expired-access",
            refresh_token=None,
            expires_at=time.time() - 60,
            tenant_id="tenant",
            client_id="client",
        ),
    )
    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            token_path=token_path,
            ms_tenant_id="tenant",
            ms_client_id="client",
        )
    )
    with patch("usiscm_ingest.microsoft.device_code_login", side_effect=AssertionError("device login")) as device:
        with pytest.raises(UnattendedAuthError, match="will not wait for a device login"):
            client.login()
    device.assert_not_called()


def test_rejected_refresh_token_exits_without_device_login(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("USISCM_INGEST_API_KEY", raising=False)
    client = _microsoft_client(tmp_path)
    rejected = MagicMock()
    rejected.ok = False
    rejected.status_code = 400
    rejected.text = "invalid_grant"
    rejected.json.return_value = {"error": "invalid_grant", "error_description": "refresh token revoked"}

    def handler(request):
        return _json_response(401, {"error": "authentication required"}, request.url)

    client.session.mount("https://", _AuthAdapter(handler))
    with patch("usiscm_ingest.microsoft.device_code_login", side_effect=AssertionError("device login")) as device:
        with patch("usiscm_ingest.microsoft.requests.post", return_value=rejected):
            with pytest.raises(UnattendedAuthError, match="rejected the refresh token"):
                client.session.post(client._url("api/v1/jobs/job-1/drawings"), json={"item": {}}, timeout=5)
    device.assert_not_called()


def test_watch_exits_when_microsoft_refresh_cannot_continue(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("USISCM_PROCESSED_DIR", str(tmp_path / "processed"))
    monkeypatch.setenv("USISCM_FAILED_DIR", str(tmp_path / "failed"))
    monkeypatch.setenv("USISCM_LEAVE_IN_PLACE", "1")
    drop = tmp_path / "ACCDocs"
    package = drop / "26092"
    package.mkdir(parents=True)
    (package / "M-101.pdf").write_bytes(b"%PDF-1.4")
    from usiscm_ingest.cli import main

    client = MagicMock()
    client.import_package.side_effect = UnattendedAuthError(
        "Microsoft rejected the refresh token. Night ingest will exit and will not wait for a device login."
    )
    with patch("usiscm_ingest.cli.UsiscmClient", return_value=client):
        with patch("usiscm_ingest.microsoft.device_code_login", side_effect=AssertionError("device login")) as device:
            code = main(["watch", str(drop), "--once", "--reprocess", "--package", "26092"])
    device.assert_not_called()
    assert code == 2
    client.import_package.assert_called_once()
