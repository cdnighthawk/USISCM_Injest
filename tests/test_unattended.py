from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from usiscm_ingest.classify import FileCategory
from usiscm_ingest.client import UsiscmClient
from usiscm_ingest.config import Settings
from usiscm_ingest.microsoft import UNATTENDED_HINT, MicrosoftAuthError, resolve_access_token
from usiscm_ingest.package import ingest_source


def test_ingest_key_never_calls_microsoft(tmp_path: Path) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "A-101.pdf").write_bytes(b"pdf")
    (root / "Project Manual.pdf").write_bytes(b"spec")
    manifest = ingest_source(root)

    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ingest_api_key="night-key",
            token_path=tmp_path / "ms_tokens.json",
            sheet_ai=False,
        )
    )
    client.resolve_project = MagicMock(return_value={"id": "job-1", "name": "Job"})  # type: ignore[method-assign]

    def fake_post(url, **kwargs):
        response = MagicMock()
        response.ok = True
        response.status_code = 201
        response.json.return_value = {"document": {}, "count": 1}
        return response

    with patch("usiscm_ingest.microsoft.discover_entra_app") as discover:
        client.session.post = MagicMock(side_effect=fake_post)
        result = client.import_package(manifest, project_id="job-1")
        discover.assert_not_called()

    urls = [call.args[0] for call in client.session.post.call_args_list]
    assert any(url.endswith("/api/drawings") for url in urls)
    assert any(url.endswith("/api/documents") for url in urls)
    assert not any("/api/v1/" in url for url in urls)
    assert result.imported >= 1
    assert FileCategory.DRAWING.value in manifest.counts
    drawings_call = next(call for call in client.session.post.call_args_list if call.args[0].endswith("/api/drawings"))
    metadata = drawings_call.kwargs["data"]["metadata"]
    assert "A-101" in metadata


def test_unattended_microsoft_does_not_prompt(tmp_path: Path) -> None:
    with pytest.raises(MicrosoftAuthError, match="Night jobs cannot wait"):
        resolve_access_token(
            MagicMock(),
            token_path=tmp_path / "missing.json",
            interactive=False,
        )
    assert "USISCM_INGEST_API_KEY" in UNATTENDED_HINT
