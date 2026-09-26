"""Enqueue shape, queue layout, and isolation from the upload path."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from usiscm_ingest.client import UsiscmClient
from usiscm_ingest.config import Settings
from usiscm_ingest.package import ingest_source
from usiscm_ingest.specialty_takeoff import (
    ALL_SPECIALTIES,
    DEFAULT_QUEUE_ROOT,
    SCHEMA_ID,
    SpecialtyTakeoffNotReady,
    claim_job,
    enqueue_specialty_takeoff,
    iter_ready,
    mark_done,
    patch_job_folder_path,
    queue_root,
)

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_default_queue_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USIS_SPECIALTY_TAKEOFF_QUEUE", raising=False)
    assert queue_root() == DEFAULT_QUEUE_ROOT
    assert queue_root() == Path(r"C:\usis-cm\data\queues\specialty_takeoff")


def test_enqueue_shape_layout_and_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_root = tmp_path / "from-env"
    monkeypatch.setenv("USIS_SPECIALTY_TAKEOFF_QUEUE", str(env_root))
    monkeypatch.setattr(
        "usiscm_ingest.specialty_takeoff._utc_now_iso",
        lambda: "2026-09-26T09:48:00Z",
    )

    path = enqueue_specialty_takeoff(
        project_key="HO-2026-0042",
        project_id="proj_abc123",
        estimate_id=None,
        folder_path=None,
        source_paths=[r"C:\Users\CharlesDossett\DC\ACCDocs\HO-2026-0042\plans.pdf"],
        specialties=["all"],
        trigger="ingest_ok",
        file_ids=["draw-1"],
        extra={"batch_id": "batch-1"},
    )

    assert path is not None
    assert path.parent == env_root / "queued"
    for stage in ("queued", "processing", "done", "failed"):
        assert (env_root / stage).is_dir()
    assert not list(env_root.rglob("*.tmp"))

    job = json.loads(path.read_text(encoding="utf-8"))
    assert job["schema"] == SCHEMA_ID
    assert job["job_id"] == path.stem
    uuid.UUID(job["job_id"])
    assert job["project_key"] == "HO-2026-0042"
    assert job["cm_ids"] == {"project_id": "proj_abc123", "estimate_id": None}
    assert job["source_paths"] == [r"C:\Users\CharlesDossett\DC\ACCDocs\HO-2026-0042\plans.pdf"]
    assert job["specialties"] == ["all"]
    assert job["folder_path"] is None
    assert job["estimate_folder"] is None
    assert job["enqueued_at"] == "2026-09-26T09:48:00Z"
    assert _ISO_Z.match(job["enqueued_at"])
    assert job["status"] == "queued"
    assert job["trigger"] == "ingest_ok"
    assert job["file_ids"] == ["draw-1"]
    assert job["source"] == "usiscm_ingest"
    assert job["batch_id"] == "batch-1"
    assert job["error"] is None
    assert not (env_root / "queue.jsonl").exists()


def test_root_argument_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USIS_SPECIALTY_TAKEOFF_QUEUE", str(tmp_path / "from-env"))
    arg_root = tmp_path / "from-arg"
    path = enqueue_specialty_takeoff(project_key="P", source_paths=["a.pdf"], root=arg_root)
    assert path is not None
    assert path.parent == arg_root / "queued"
    assert not (tmp_path / "from-env" / "queued").exists()


def test_specialties_all_stays_collapsed_until_claim(tmp_path: Path) -> None:
    nine = enqueue_specialty_takeoff(
        project_key="P",
        specialties=list(ALL_SPECIALTIES),
        folder_path=r"C:\estimates\P",
        root=tmp_path / "nine",
    )
    assert nine is not None
    assert json.loads(nine.read_text(encoding="utf-8"))["specialties"] == list(ALL_SPECIALTIES)

    collapsed = enqueue_specialty_takeoff(
        project_key="P",
        specialties=["lockers", "ALL"],
        root=tmp_path / "all",
    )
    assert collapsed is not None
    assert json.loads(collapsed.read_text(encoding="utf-8"))["specialties"] == ["all"]


def test_enqueue_swallows_write_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path: Path, job: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("usiscm_ingest.specialty_takeoff._write_job", boom)
    assert enqueue_specialty_takeoff(project_key="P", source_paths=["a.pdf"]) is None


def test_patch_then_claim_expands_all(specialty_takeoff_queue: Path) -> None:
    queued = enqueue_specialty_takeoff(
        project_key="HO-2026-0042",
        project_id="proj_abc123",
        source_paths=["plans.pdf"],
        folder_path=None,
    )
    assert queued is not None
    job = json.loads(queued.read_text(encoding="utf-8"))
    assert list(iter_ready()) == []
    with pytest.raises(SpecialtyTakeoffNotReady):
        claim_job(job["job_id"], claimed_by="lockers")
    assert queued.is_file()

    assert patch_job_folder_path(queued, r"C:\CM\Estimates\HO-2026-0042")
    patched = json.loads(queued.read_text(encoding="utf-8"))
    assert patched["folder_path"] == r"C:\CM\Estimates\HO-2026-0042"
    assert patched["estimate_folder"] == patched["folder_path"]
    assert patched["status"] == "ready_for_takeoff"
    assert _ISO_Z.match(patched["folder_path_patched_at"])
    ready = list(iter_ready())
    assert len(ready) == 1

    claimed = claim_job(job["job_id"], claimed_by="lockers")
    assert not queued.exists()
    assert claimed["status"] == "processing"
    assert claimed["claimed_by"] == "lockers"
    assert claimed["specialties"] == list(ALL_SPECIALTIES)
    assert (specialty_takeoff_queue / "processing" / f"{job['job_id']}.json").is_file()

    finished = mark_done(job["job_id"])
    assert finished["status"] == "done"
    assert (specialty_takeoff_queue / "done" / f"{job['job_id']}.json").is_file()
    assert not (specialty_takeoff_queue / "processing" / f"{job['job_id']}.json").exists()


def _client(tmp_path: Path, *, project: dict | None = None) -> tuple[UsiscmClient, object]:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "A-101.pdf").write_bytes(b"%PDF-1.4 drawing")
    (root / "Project Manual.pdf").write_bytes(b"spec")
    manifest = ingest_source(root)
    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ms_access_token="ms-token",
            token_path=tmp_path / "ms.json",
            sheet_ai=False,
        ),
        sleeper=lambda _: None,
        b2_post=lambda hint, payload, **kwargs: {"fileId": "fid", "fileName": hint.get("file_name"), "contentSha1": "abc"},
    )
    client.token = "ms-token"
    client.auth_mode = "microsoft"
    client.resolve_project = MagicMock(  # type: ignore[method-assign]
        return_value=project
        or {
            "id": "job-uuid",
            "name": "Harbor Office",
            "kind": "job",
            "job_id": "job-uuid",
            "number": "HO-2026-0042",
            "estimate_id": "est-9",
        }
    )
    return client, manifest


def _ok_response(url: str, **kwargs: object) -> MagicMock:
    response = MagicMock()
    response.ok = True
    response.headers = {}
    response.status_code = 201
    if "/jobs/" in url and str(url).endswith("/drawings"):
        response.json.return_value = {
            "item": {"id": "draw-1"},
            "upload": {
                "mode": "b2_native",
                "url": "https://pod-000.backblaze.com/b2api/v2/b2_upload_file/x",
                "authorization": "b2tok",
                "file_name": "drawings/A-101.pdf",
            },
        }
    elif str(url).endswith("/ack-file"):
        response.status_code = 200
        response.json.return_value = {"item": {"id": "draw-1", "file_pending": False}}
    elif str(url).endswith("/ingest/files"):
        response.json.return_value = {"document": {"id": "doc-9"}}
    else:
        response.status_code = 200
        response.json.return_value = {"item": {}}
    return response


def test_import_enqueues_one_job_for_the_batch(tmp_path: Path, specialty_takeoff_queue: Path) -> None:
    client, manifest = _client(tmp_path)
    client.session.post = MagicMock(side_effect=_ok_response)

    result = client.import_package(manifest, project_id="job-uuid")

    assert result.imported == 2
    assert result.errors == []
    jobs = list((specialty_takeoff_queue / "queued").glob("*.json"))
    assert len(jobs) == 1
    job = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert job["schema"] == SCHEMA_ID
    assert job["status"] == "queued"
    assert job["trigger"] == "ingest_ok"
    assert job["project_key"] == "HO-2026-0042"
    assert job["cm_ids"]["project_id"] == "job-uuid"
    assert job["cm_ids"]["estimate_id"] == "est-9"
    assert job["folder_path"] is None
    assert job["file_ids"] == ["draw-1"]
    assert job["batch_id"] == result.batch_id
    assert job["source"] == "usiscm_ingest"
    assert len(job["source_paths"]) == 2
    assert any(path.endswith("A-101.pdf") for path in job["source_paths"])
    assert any(path.endswith("Project Manual.pdf") for path in job["source_paths"])
    assert "Y:" not in json.dumps(job)


def test_dry_run_does_not_enqueue(tmp_path: Path, specialty_takeoff_queue: Path) -> None:
    client, manifest = _client(tmp_path)
    client.session.post = MagicMock()
    result = client.import_package(manifest, project_id="job-uuid", dry_run=True)
    assert result.imported >= 1
    client.session.post.assert_not_called()
    assert not (specialty_takeoff_queue / "queued").exists()


def test_upload_error_does_not_enqueue(tmp_path: Path, specialty_takeoff_queue: Path) -> None:
    client, manifest = _client(tmp_path)

    def fake_post(url, **kwargs):
        response = _ok_response(url, **kwargs)
        body = kwargs.get("json") or {}
        item = body.get("item") if isinstance(body, dict) else None
        if isinstance(item, dict) and str(item.get("sourceFileName") or "").startswith("A-"):
            response.ok = False
            response.status_code = 500
            response.json.return_value = {"error": "catalog down"}
        return response

    client.session.post = MagicMock(side_effect=fake_post)
    result = client.import_package(manifest, project_id="job-uuid")
    assert result.errors
    assert not (specialty_takeoff_queue / "queued").exists() or not list(
        (specialty_takeoff_queue / "queued").glob("*.json")
    )


def test_import_succeeds_when_queue_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("USIS_SPECIALTY_TAKEOFF_QUEUE", str(blocker))
    client, manifest = _client(tmp_path)
    client.session.post = MagicMock(side_effect=_ok_response)

    result = client.import_package(manifest, project_id="job-uuid")

    assert result.imported == 2
    assert result.errors == []


def test_import_succeeds_when_enqueue_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs: object) -> None:
        raise RuntimeError("queue down")

    monkeypatch.setattr("usiscm_ingest.specialty_takeoff.enqueue_specialty_takeoff", boom)
    client, manifest = _client(tmp_path)
    client.session.post = MagicMock(side_effect=_ok_response)

    result = client.import_package(manifest, project_id="job-uuid")

    assert result.imported == 2
    assert result.errors == []
