"""Serial specialty runner: one project, then one script, never overlapping."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from usiscm_ingest.cli import main
from usiscm_ingest.specialty_runner import (
    RUNNER_ID,
    ConfiguredScripts,
    ScriptSpec,
    SpecialtyRunnerBusy,
    load_script_specs,
    run_queue,
    script_order,
)
from usiscm_ingest.specialty_takeoff import ALL_SPECIALTIES, SCHEMA_ID, claim_job

SLUGS = ["lockers", "concrete", "door_spec"]


class RecordingScripts:
    """In-process plug-in. Records start/end and flags any overlap."""

    def __init__(
        self,
        *,
        waiting: Path | None = None,
        hold_project: str | None = None,
        fail: set[tuple[str, str]] | None = None,
    ) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.active = 0
        self.max_active = 0
        self.overlap = False
        self.waiting_left = False
        self.waiting = waiting
        self.hold_project = hold_project
        self.fail = fail or set()
        self.folders: list[str] = []

    def supports(self, specialty: str) -> bool:
        return True

    def run(self, specialty: str, job: dict, *, job_path: Path) -> None:
        self.active += 1
        if self.active > 1:
            self.overlap = True
        self.max_active = max(self.max_active, self.active)
        project = str(job.get("project_key"))
        self.events.append(("start", project, specialty))
        processing = list(job_path.parent.glob("*.json"))
        if len(processing) != 1 or processing[0].stem != str(job.get("job_id")):
            self.overlap = True
        try:
            folder = str(job.get("folder_path") or "")
            self.folders.append(folder)
            if folder.startswith("Y:"):
                raise RuntimeError("invented estimate root")
            if self.hold_project and project == self.hold_project and self.waiting is not None:
                if not self.waiting.is_file():
                    self.waiting_left = True
            if (project, specialty) in self.fail:
                raise RuntimeError("boom")
        finally:
            self.events.append(("end", project, specialty))
            self.active -= 1


def _write_queued(
    root: Path,
    *,
    job_id: str,
    project_key: str,
    enqueued_at: str,
    folder: str | None,
    specialties: list[str],
) -> Path:
    payload = {
        "schema": SCHEMA_ID,
        "job_id": job_id,
        "project_key": project_key,
        "cm_ids": {"project_id": project_key, "estimate_id": None},
        "source_paths": [rf"C:\acc\{project_key}\plans.pdf"],
        "specialties": specialties,
        "folder_path": folder,
        "estimate_folder": folder,
        "enqueued_at": enqueued_at,
        "status": "queued",
        "trigger": "ingest_ok",
        "file_ids": [],
        "error": None,
        "claimed_at": None,
        "claimed_by": None,
        "finished_at": None,
        "source": "usiscm_ingest",
    }
    path = root / "queued" / f"{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _starts(events: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    return [(project, specialty) for kind, project, specialty in events if kind == "start"]


def test_script_order_expands_all() -> None:
    assert script_order({"specialties": ["all"]}) == list(ALL_SPECIALTIES)
    assert script_order({"specialties": ["fec", "lockers", "fec"]}) == ["fec", "lockers"]


def test_projects_and_scripts_are_strictly_serial(tmp_path: Path) -> None:
    # Filename order would pick aaa-b first. enqueued_at must win.
    _write_queued(
        tmp_path,
        job_id="zzz-a",
        project_key="A",
        enqueued_at="2026-09-26T01:00:00Z",
        folder=r"C:\CM\Estimates\A",
        specialties=SLUGS,
    )
    waiting = _write_queued(
        tmp_path,
        job_id="aaa-b",
        project_key="B",
        enqueued_at="2026-09-26T02:00:00Z",
        folder=r"C:\CM\Estimates\B",
        specialties=SLUGS,
    )
    scripts = RecordingScripts(waiting=waiting, hold_project="A")
    report = run_queue(scripts, queue_root=tmp_path)

    assert _starts(scripts.events) == [
        ("A", "lockers"),
        ("A", "concrete"),
        ("A", "door_spec"),
        ("B", "lockers"),
        ("B", "concrete"),
        ("B", "door_spec"),
    ]
    assert [event[0] for event in scripts.events] == ["start", "end"] * 6
    assert scripts.max_active == 1
    assert scripts.overlap is False
    assert scripts.waiting_left is False
    assert [project.project_key for project in report.projects] == ["A", "B"]
    assert all(project.status == "done" for project in report.projects)
    done_a = json.loads((tmp_path / "done" / "zzz-a.json").read_text(encoding="utf-8"))
    assert [row["specialty"] for row in done_a["specialty_results"]] == SLUGS
    assert all(row["status"] == "ok" for row in done_a["specialty_results"])
    assert done_a["claimed_by"] == RUNNER_ID
    assert not str(done_a["folder_path"]).startswith("Y:")


def test_all_nine_run_in_order_for_one_project(tmp_path: Path) -> None:
    _write_queued(
        tmp_path,
        job_id="only",
        project_key="Only",
        enqueued_at="2026-09-26T01:00:00Z",
        folder=r"C:\CM\Estimates\Only",
        specialties=["all"],
    )
    scripts = RecordingScripts()
    run_queue(scripts, queue_root=tmp_path)
    assert _starts(scripts.events) == [("Only", slug) for slug in ALL_SPECIALTIES]
    assert scripts.max_active == 1


def test_null_folder_waits_and_does_not_start_the_next_project(tmp_path: Path) -> None:
    _write_queued(
        tmp_path,
        job_id="zzz-a",
        project_key="A",
        enqueued_at="2026-09-26T01:00:00Z",
        folder=None,
        specialties=SLUGS,
    )
    _write_queued(
        tmp_path,
        job_id="aaa-b",
        project_key="B",
        enqueued_at="2026-09-26T02:00:00Z",
        folder=r"C:\CM\Estimates\B",
        specialties=SLUGS,
    )
    scripts = RecordingScripts()
    report = run_queue(scripts, queue_root=tmp_path)

    assert scripts.events == []
    assert report.projects == []
    assert report.waiting_job_id == "zzz-a"
    assert report.skipped_null_folder == 1
    waiting = json.loads((tmp_path / "queued" / "zzz-a.json").read_text(encoding="utf-8"))
    assert waiting["folder_path"] is None
    assert waiting["status"] == "queued"
    assert (tmp_path / "queued" / "aaa-b.json").is_file()
    assert "Y:" not in json.dumps(waiting)


def test_failed_script_still_finishes_the_project_before_the_next(tmp_path: Path) -> None:
    _write_queued(
        tmp_path,
        job_id="zzz-a",
        project_key="A",
        enqueued_at="2026-09-26T01:00:00Z",
        folder=r"C:\CM\Estimates\A",
        specialties=SLUGS,
    )
    waiting = _write_queued(
        tmp_path,
        job_id="aaa-b",
        project_key="B",
        enqueued_at="2026-09-26T02:00:00Z",
        folder=r"C:\CM\Estimates\B",
        specialties=SLUGS,
    )
    scripts = RecordingScripts(waiting=waiting, hold_project="A", fail={("A", "concrete")})
    report = run_queue(scripts, queue_root=tmp_path)

    assert _starts(scripts.events) == [(project, slug) for project in ("A", "B") for slug in SLUGS]
    assert scripts.max_active == 1
    assert scripts.waiting_left is False
    assert [project.status for project in report.projects] == ["failed", "done"]
    failed = json.loads((tmp_path / "failed" / "zzz-a.json").read_text(encoding="utf-8"))
    assert "concrete" in failed["error"]
    assert (tmp_path / "done" / "aaa-b.json").is_file()


def test_in_progress_project_finishes_before_a_queued_one(tmp_path: Path) -> None:
    _write_queued(
        tmp_path,
        job_id="zzz-a",
        project_key="A",
        enqueued_at="2026-09-26T01:00:00Z",
        folder=r"C:\CM\Estimates\A",
        specialties=SLUGS,
    )
    claim_job("zzz-a", claimed_by="earlier", queue_root=tmp_path)
    processing = tmp_path / "processing" / "zzz-a.json"
    job = json.loads(processing.read_text(encoding="utf-8"))
    job["specialty_results"] = [
        {
            "specialty": "lockers",
            "status": "ok",
            "started_at": "2026-09-26T01:01:00Z",
            "finished_at": "2026-09-26T01:02:00Z",
            "error": None,
        }
    ]
    processing.write_text(json.dumps(job), encoding="utf-8")
    _write_queued(
        tmp_path,
        job_id="aaa-b",
        project_key="B",
        enqueued_at="2026-09-26T02:00:00Z",
        folder=r"C:\CM\Estimates\B",
        specialties=SLUGS,
    )
    scripts = RecordingScripts()
    run_queue(scripts, queue_root=tmp_path)
    assert _starts(scripts.events) == [
        ("A", "concrete"),
        ("A", "door_spec"),
        ("B", "lockers"),
        ("B", "concrete"),
        ("B", "door_spec"),
    ]
    assert scripts.max_active == 1


def test_missing_runner_does_not_skip_to_the_next_project(tmp_path: Path) -> None:
    _write_queued(
        tmp_path,
        job_id="zzz-a",
        project_key="A",
        enqueued_at="2026-09-26T01:00:00Z",
        folder=r"C:\CM\Estimates\A",
        specialties=SLUGS,
    )
    _write_queued(
        tmp_path,
        job_id="aaa-b",
        project_key="B",
        enqueued_at="2026-09-26T02:00:00Z",
        folder=r"C:\CM\Estimates\B",
        specialties=["lockers"],
    )
    scripts = ConfiguredScripts({"lockers": ScriptSpec(specialty="lockers", module="not.used:run")})
    report = run_queue(scripts, queue_root=tmp_path)
    assert report.projects == []
    assert report.blocked_job_id == "zzz-a"
    assert "concrete" in (report.blocked_reason or "")
    assert (tmp_path / "queued" / "zzz-a.json").is_file()
    assert (tmp_path / "queued" / "aaa-b.json").is_file()


def test_second_runner_is_busy_until_the_first_finishes(tmp_path: Path) -> None:
    _write_queued(
        tmp_path,
        job_id="only",
        project_key="Only",
        enqueued_at="2026-09-26T01:00:00Z",
        folder=r"C:\CM\Estimates\Only",
        specialties=["lockers"],
    )

    class Nested(RecordingScripts):
        def run(self, specialty: str, job: dict, *, job_path: Path) -> None:
            with pytest.raises(SpecialtyRunnerBusy):
                run_queue(self, queue_root=tmp_path)
            super().run(specialty, job, job_path=job_path)

    scripts = Nested()
    report = run_queue(scripts, queue_root=tmp_path)
    assert report.projects[0].status == "done"
    assert scripts.max_active == 1
    assert not (tmp_path / "runner.lock").exists()


def test_max_jobs_stops_before_the_next_project(tmp_path: Path) -> None:
    _write_queued(
        tmp_path,
        job_id="zzz-a",
        project_key="A",
        enqueued_at="2026-09-26T01:00:00Z",
        folder=r"C:\CM\Estimates\A",
        specialties=["lockers"],
    )
    _write_queued(
        tmp_path,
        job_id="aaa-b",
        project_key="B",
        enqueued_at="2026-09-26T02:00:00Z",
        folder=r"C:\CM\Estimates\B",
        specialties=["lockers"],
    )
    scripts = RecordingScripts()
    report = run_queue(scripts, queue_root=tmp_path, max_jobs=1)
    assert [project.project_key for project in report.projects] == ["A"]
    assert (tmp_path / "queued" / "aaa-b.json").is_file()
    assert scripts.max_active == 1


def test_load_script_specs_command_and_module(tmp_path: Path) -> None:
    path = tmp_path / "runners.yaml"
    path.write_text(
        """
runners:
  lockers:
    command: ["python", "lockers.py", "--folder", "{folder_path}"]
  concrete:
    module: estimating_specialties.concrete:run
  door_spec: estimating_specialties.door_spec:run
""",
        encoding="utf-8",
    )
    specs = load_script_specs(path)
    assert specs["lockers"].command == ("python", "lockers.py", "--folder", "{folder_path}")
    assert specs["concrete"].module == "estimating_specialties.concrete:run"
    assert specs["door_spec"].module == "estimating_specialties.door_spec:run"
    path.write_text(
        "runners:\n  lockers:\n    module: a:b\n    command: ['x']\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_script_specs(path)


def test_cli_once_runs_commands_in_order(tmp_path: Path, specialty_takeoff_queue: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USIS_SPECIALTY_RUNNERS", raising=False)
    log = tmp_path / "ran.txt"
    code = (
        "import os\n"
        "from pathlib import Path\n"
        f"p = Path({str(log)!r})\n"
        "prev = p.read_text(encoding='utf-8') if p.exists() else ''\n"
        "line = os.environ['USIS_SPECIALTY'] + ' ' + os.environ['USIS_SPECIALTY_FOLDER_PATH']\n"
        "if line.startswith('Y:') or os.environ['USIS_SPECIALTY_FOLDER_PATH'].startswith('Y:'):\n"
        "    raise SystemExit(2)\n"
        "p.write_text(prev + line + '\\n', encoding='utf-8')\n"
    )
    runners = tmp_path / "runners.yaml"
    runners.write_text(
        yaml.safe_dump(
            {
                "runners": {
                    slug: {"command": [sys.executable, "-c", code]}
                    for slug in ("lockers", "concrete")
                }
            }
        ),
        encoding="utf-8",
    )
    _write_queued(
        specialty_takeoff_queue,
        job_id="p1",
        project_key="P",
        enqueued_at="2026-09-26T01:00:00Z",
        folder=r"C:\CM\Estimates\P",
        specialties=["lockers", "concrete"],
    )
    assert main(["specialty-run", "--once", "--runners", str(runners)]) == 0
    assert log.read_text(encoding="utf-8").splitlines() == [
        r"lockers C:\CM\Estimates\P",
        r"concrete C:\CM\Estimates\P",
    ]
    done = json.loads((specialty_takeoff_queue / "done" / "p1.json").read_text(encoding="utf-8"))
    assert done["status"] == "done"
    assert done["folder_path"] == r"C:\CM\Estimates\P"


def test_cli_without_runners_does_not_claim(tmp_path: Path, specialty_takeoff_queue: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USIS_SPECIALTY_RUNNERS", raising=False)
    _write_queued(
        specialty_takeoff_queue,
        job_id="p1",
        project_key="P",
        enqueued_at="2026-09-26T01:00:00Z",
        folder=r"C:\CM\Estimates\P",
        specialties=["lockers"],
    )
    assert main(["specialty-run", "--once"]) == 2
    assert (specialty_takeoff_queue / "queued" / "p1.json").is_file()
    processing = specialty_takeoff_queue / "processing"
    assert not processing.exists() or not list(processing.glob("*.json"))


def test_successful_import_enqueues_and_runner_waits(tmp_path: Path, specialty_takeoff_queue: Path) -> None:
    from tests.test_specialty_takeoff import _client, _ok_response

    client, manifest = _client(tmp_path)
    client.session.post = MagicMock(side_effect=_ok_response)
    result = client.import_package(manifest, project_id="job-uuid")
    assert result.imported == 2
    assert result.errors == []

    scripts = RecordingScripts()
    report = run_queue(scripts, queue_root=specialty_takeoff_queue)
    assert scripts.events == []
    assert report.projects == []
    assert report.waiting_job_id
    job = json.loads(next((specialty_takeoff_queue / "queued").glob("*.json")).read_text(encoding="utf-8"))
    assert job["schema"] == SCHEMA_ID
    assert job["folder_path"] is None
    assert job["specialties"] == ["all"]
    assert job["trigger"] == "ingest_ok"
    assert job["status"] == "queued"


def test_import_does_not_start_the_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_specialty_takeoff import _client, _ok_response

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("specialty runner must not run inside ingest")

    monkeypatch.setattr("usiscm_ingest.specialty_runner.run_queue", boom)
    client, manifest = _client(tmp_path)
    client.session.post = MagicMock(side_effect=_ok_response)
    result = client.import_package(manifest, project_id="job-uuid")
    assert result.imported == 2
    assert result.errors == []
