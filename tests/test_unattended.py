from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from usiscm_ingest.classify import FileCategory
from usiscm_ingest.client import UsiscmClient
from usiscm_ingest.config import Settings
from usiscm_ingest.microsoft import UNATTENDED_HINT, MicrosoftAuthError, resolve_access_token
from usiscm_ingest.package import ingest_source


def _b2_hint(file_name: str) -> dict:
    return {
        "mode": "b2_native",
        "url": "https://pod.backblaze.com/b2api/v2/b2_upload_file/x",
        "authorization": "b2tok",
        "file_name": file_name,
    }


def test_ingest_key_never_calls_microsoft(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "A-101.pdf").write_bytes(b"%PDF drawing")
    (root / "Project Manual.pdf").write_bytes(b"spec-bytes")
    manifest = ingest_source(root)

    b2_payloads: list[bytes] = []

    def fake_b2(hint, payload, **kwargs):
        b2_payloads.append(payload)
        assert kwargs.get("content_type")
        return {"fileId": "fid", "fileName": hint.get("file_name"), "contentSha1": "abc"}

    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ingest_api_key="night-key",
            token_path=tmp_path / "ms_tokens.json",
            sheet_ai=False,
        ),
        b2_post=fake_b2,
    )
    client.resolve_project = MagicMock(return_value={"id": "job-1", "name": "Job"})  # type: ignore[method-assign]

    def fake_post(url, **kwargs):
        assert "files" not in kwargs
        response = MagicMock()
        response.ok = True
        response.status_code = 201
        response.headers = {}
        if url.endswith("/api/drawings"):
            response.json.return_value = {
                "drawing": {"id": "draw-1", "file_pending": True},
                "upload": _b2_hint("drawings/A-101.pdf"),
            }
        elif url.endswith("/api/documents"):
            body = kwargs.get("json") or {}
            assert body.get("document_type") == "specification"
            assert body.get("content_hash")
            assert "spec-bytes" not in str(body)
            response.json.return_value = {
                "document": {"id": "doc-1", "file_pending": True},
                "upload": _b2_hint("documents/Project-Manual.pdf"),
            }
        elif url.endswith("/ack-file"):
            response.status_code = 200
            response.json.return_value = {"item": {"file_pending": False}}
        else:
            response.json.return_value = {}
        return response

    with patch("usiscm_ingest.microsoft.discover_entra_app") as discover:
        client.session.post = MagicMock(side_effect=fake_post)
        result = client.import_package(manifest, project_id="job-1")
        discover.assert_not_called()

    urls = [call.args[0] for call in client.session.post.call_args_list]
    assert any(url.endswith("/api/drawings") for url in urls)
    assert any(url.endswith("/api/documents") for url in urls)
    assert any("/api/drawings/draw-1/ack-file" in url for url in urls)
    assert any("/api/documents/doc-1/ack-file" in url for url in urls)
    assert not any("/api/v1/" in url for url in urls)
    assert result.imported == 2
    assert result.errors == []
    assert FileCategory.DRAWING.value in manifest.counts
    drawings_call = next(call for call in client.session.post.call_args_list if call.args[0].endswith("/api/drawings"))
    metadata = drawings_call.kwargs["json"]
    assert metadata["sheet_number"] == "A-101"
    assert b"%PDF drawing" in b2_payloads
    assert b"spec-bytes" in b2_payloads


def test_unattended_microsoft_does_not_prompt(tmp_path: Path) -> None:
    with pytest.raises(MicrosoftAuthError, match="Night jobs cannot wait"):
        resolve_access_token(
            MagicMock(),
            token_path=tmp_path / "missing.json",
            interactive=False,
        )
    assert "USISCM_INGEST_API_KEY" in UNATTENDED_HINT
