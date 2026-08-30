from pathlib import Path
from unittest.mock import MagicMock

from usiscm_ingest.classify import FileCategory
from usiscm_ingest.client import UsiscmClient, _best_project_match
from usiscm_ingest.config import Settings
from usiscm_ingest.package import ingest_source


def _client(tmp_path: Path) -> UsiscmClient:
    return UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ms_access_token="ms-token",
            token_path=tmp_path / "ms_tokens.json",
        )
    )


def test_import_package_dry_run_does_not_post(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "A-101.pdf").write_bytes(b"x")
    manifest = ingest_source(root)
    client = _client(tmp_path)
    client.resolve_project = MagicMock(return_value={"id": "proj-1", "name": "Job"})  # type: ignore[method-assign]
    client.session.post = MagicMock()

    result = client.import_package(manifest, project_id="proj-1", dry_run=True)

    client.session.post.assert_not_called()
    assert result.project_id == "proj-1"
    assert result.imported == 1


def test_import_routes_drawings_and_docs_to_v1_and_ingest(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "A-101.pdf").write_bytes(b"pdf")
    (root / "Project Manual.pdf").write_bytes(b"spec")
    manifest = ingest_source(root)
    assert {item.category for item in manifest.files} >= {FileCategory.DRAWING, FileCategory.SPEC}

    client = _client(tmp_path)
    client.token = "ms-token"
    client.session.headers["Authorization"] = "Bearer ms-token"
    client.resolve_project = MagicMock(return_value={"id": "abc-uuid", "name": "Job"})  # type: ignore[method-assign]

    def fake_post(url, **kwargs):
        response = MagicMock()
        response.ok = True
        response.status_code = 201
        response.json.return_value = {"item": {}, "count": 1}
        return response

    client.session.post = MagicMock(side_effect=fake_post)
    result = client.import_package(manifest, project_id="abc-uuid")
    urls = [call.args[0] for call in client.session.post.call_args_list]
    assert any("/api/v1/projects/abc-uuid/drawings" in url for url in urls)
    assert any("/api/v1/projects/abc-uuid/spec-book/import" in url for url in urls)
    assert any(url.endswith("/api/documents") for url in urls)
    assert result.imported >= 1


def test_project_match_uses_v1_name_field() -> None:
    projects = [
        {"id": "1", "name": "Kaiser Permanente San Rafael MOB", "number": "KP-SR-01"},
        {"id": "2", "name": "Sutter Oakland", "number": "SO-9"},
    ]
    match = _best_project_match("Kaiser Permanente San Rafael", projects)
    assert match is not None
    assert match["id"] == "1"
