from pathlib import Path
from unittest.mock import MagicMock

from usiscm_ingest.classify import FileCategory
from usiscm_ingest.client import UsiscmClient
from usiscm_ingest.package import PackageManifest, ingest_source


def test_import_package_dry_run_does_not_post(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    (root).mkdir()
    (root / "A-101.pdf").write_bytes(b"x")
    manifest = ingest_source(root)
    client = UsiscmClient("https://www.usiscm.com", "user@example.com", "secret")
    client.resolve_project = MagicMock(return_value={"id": 9, "project_name": "Job"})  # type: ignore[method-assign]
    client.session.post = MagicMock()

    result = client.import_package(manifest, project_id=9, dry_run=True)

    client.session.post.assert_not_called()
    assert result.project_id == 9
    assert result.imported == 1


def test_import_routes_categories_to_expected_endpoints(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    (root / "drawings").mkdir(parents=True)
    (root / "A-101.pdf").write_bytes(b"pdf")
    (root / "detail.dwg").write_bytes(b"cad")
    (root / "Project Manual.pdf").write_bytes(b"spec")
    manifest = ingest_source(root)
    assert {item.category for item in manifest.files} >= {FileCategory.DRAWING, FileCategory.SPEC}

    client = UsiscmClient("https://www.usiscm.com", "user@example.com", "secret")
    client.token = "tok"
    client.session.headers["Authorization"] = "Bearer tok"
    client.resolve_project = MagicMock(return_value={"id": 3, "project_name": "Job"})  # type: ignore[method-assign]

    def fake_post(url, **kwargs):
        response = MagicMock()
        response.ok = True
        response.status_code = 200
        if url.endswith("/drawings/import"):
            response.json.return_value = {"imported": 1, "sheets": [{}], "errors": []}
        else:
            response.json.return_value = {"imported": 1, "errors": []}
        return response

    client.session.post = MagicMock(side_effect=fake_post)
    result = client.import_package(manifest, project_id=3)
    urls = [call.args[0] for call in client.session.post.call_args_list]
    assert any(url.endswith("/api/projects/3/drawings/import") for url in urls)
    assert any("/documents/bulk" in url for url in urls)
    assert any(url.endswith("/documents/bulk-docs") for url in urls)
    assert result.imported >= 1
