"""Serial specialty-takeoff worker.

``usiscm-ingest watch`` / ``import`` only enqueue. This module drains the
queue in a separate process (``usiscm-ingest specialty-run``):

- One project at a time, FIFO by ``enqueued_at`` among jobs that are ready.
- One specialty script at a time, in slug order, for that project.
- The next project is claimed only after the current project's scripts finish.
- A null ``folder_path`` stays in ``queued/`` (pending takeoff). No estimate
  drive path is invented.

Real scripts plug in through a YAML map of specialty → command or module.
See ``usiscm-specialty-runners.example.yaml``.
"""

from __future__ import annotations

import importlib
import logging
import os
import shlex
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

from usiscm_ingest.specialty_takeoff import (
    ALL_SPECIALTIES,
    SpecialtyTakeoffNotReady,
    _utc_now_iso,
    _write_job,
    claim_job,
    ensure_queue_dirs,
    expand_specialties,
    is_ready,
    iter_queued,
    load_job,
    mark_done,
    mark_failed,
    queue_root as default_queue_root,
    resolve_folder_path,
)

logger = logging.getLogger(__name__)

RUNNER_ID = "usiscm_ingest.specialty_runner"
RUNNERS_ENV = "USIS_SPECIALTY_RUNNERS"


class SpecialtyRunnerBusy(Exception):
    """Another specialty-run process holds the queue lock."""


class SpecialtyScriptError(Exception):
    """A plugged-in specialty command or module failed."""


class SpecialtyScripts(Protocol):
    """One-at-a-time invoker. ``run`` must return before the next script starts."""

    def supports(self, specialty: str) -> bool:
        """True when this specialty has a command or module configured."""

    def run(self, specialty: str, job: dict[str, Any], *, job_path: Path) -> None:
        """Run one specialty for one project. Raise on failure."""


@dataclass(frozen=True)
class ScriptSpec:
    """Plug-in for one specialty slug."""

    specialty: str
    command: tuple[str, ...] | None = None
    module: str | None = None


@dataclass
class ProjectRun:
    job_id: str
    project_key: str | None
    status: str
    specialties: list[str]


@dataclass
class RunReport:
    projects: list[ProjectRun] = field(default_factory=list)
    skipped_null_folder: int = 0
    waiting_job_id: str | None = None
    waiting_reason: str | None = None
    blocked_job_id: str | None = None
    blocked_reason: str | None = None

    @property
    def failed(self) -> bool:
        return any(project.status == "failed" for project in self.projects)


class _FormatFields(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class ConfiguredScripts:
    """Run the command or ``module:function`` configured for each slug."""

    def __init__(self, specs: dict[str, ScriptSpec]) -> None:
        self.specs = specs

    def supports(self, specialty: str) -> bool:
        return specialty in self.specs

    def run(self, specialty: str, job: dict[str, Any], *, job_path: Path) -> None:
        spec = self.specs.get(specialty)
        if spec is None:
            raise SpecialtyScriptError(f"no runner configured for {specialty}")
        if spec.module:
            _call_module(spec.module, specialty, job)
            return
        if spec.command:
            _call_command(spec.command, specialty, job, job_path=job_path)
            return
        raise SpecialtyScriptError(f"no command or module for {specialty}")


def load_script_specs(path: Path | None) -> dict[str, ScriptSpec]:
    """Load ``runners: {slug: {command|module}}`` from YAML. Missing file → {}."""
    if path is None or not path.is_file():
        return {}
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load specialty runners") from exc
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"specialty runners file must be a mapping: {path}")
    block = raw.get("runners", raw)
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ValueError(f"runners must be a mapping: {path}")
    specs: dict[str, ScriptSpec] = {}
    for key, value in block.items():
        if value is None:
            continue
        specialty = str(key).strip()
        if not specialty:
            continue
        specs[specialty] = _parse_spec(specialty, value)
    return specs


def missing_runners(specs: dict[str, ScriptSpec], specialties: list[str]) -> list[str]:
    return [slug for slug in specialties if slug not in specs]


@contextmanager
def runner_lock(root: Path) -> Iterator[None]:
    """Exclusive lock so two workers cannot run scripts at once."""
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "runner.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise SpecialtyRunnerBusy(f"specialty runner already holds {lock_path}") from exc
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


def run_queue(
    scripts: SpecialtyScripts,
    *,
    queue_root: Path | None = None,
    max_jobs: int | None = None,
    claimed_by: str = RUNNER_ID,
) -> RunReport:
    """Drain ready jobs serially. Does not run inside the ingest watcher.

    An in-progress ``processing/`` job is finished before any queued project
    is claimed. The oldest queued job is next. If its ``folder_path`` is
    null, the worker waits and does not start a later project. A head job
    whose scripts are not all configured also blocks; it stays in ``queued/``.
    """
    root = Path(queue_root) if queue_root is not None else default_queue_root()
    report = RunReport()
    with runner_lock(root):
        finished = 0
        while max_jobs is None or finished < max_jobs:
            current = _oldest_processing(root)
            if current is None:
                nxt = _oldest_queued(root)
                if nxt is None:
                    break
                path, job = nxt
                if not is_ready(job):
                    report.waiting_job_id = str(job.get("job_id") or path.stem)
                    report.waiting_reason = "folder_path is null; pending takeoff"
                    logger.info(
                        "specialty runner waiting job_id=%s: %s",
                        report.waiting_job_id,
                        report.waiting_reason,
                    )
                    break
                slugs = script_order(job)
                if hasattr(scripts, "specs"):
                    missing = missing_runners(getattr(scripts, "specs"), slugs)
                else:
                    missing = [slug for slug in slugs if not scripts.supports(slug)]
                if missing:
                    report.blocked_job_id = str(job.get("job_id") or path.stem)
                    report.blocked_reason = "no runner configured for " + ", ".join(missing)
                    logger.error(
                        "specialty runner blocked job_id=%s: %s",
                        report.blocked_job_id,
                        report.blocked_reason,
                    )
                    break
                try:
                    claimed = claim_job(str(job.get("job_id") or path.stem), claimed_by=claimed_by, queue_root=root)
                except SpecialtyTakeoffNotReady:
                    report.waiting_job_id = str(job.get("job_id") or path.stem)
                    report.waiting_reason = "folder_path is null; pending takeoff"
                    break
                except (FileNotFoundError, FileExistsError) as exc:
                    logger.warning("specialty claim skipped: %s", exc)
                    break
                current = (root / "processing" / f"{claimed['job_id']}.json", claimed)
            outcome = _run_project(current[0], scripts, root)
            report.projects.append(outcome)
            finished += 1
        report.skipped_null_folder = _count_null_folder(root)
    return report


def script_order(job: dict[str, Any]) -> list[str]:
    """Expand ``all`` and drop duplicates, keeping first-seen order."""
    ordered: list[str] = []
    for slug in expand_specialties(job.get("specialties")):
        if slug not in ordered:
            ordered.append(slug)
    return ordered


def _fifo_key(job: dict[str, Any]) -> tuple[str, str]:
    return (str(job.get("enqueued_at") or ""), str(job.get("job_id") or ""))


def _oldest_queued(root: Path) -> tuple[Path, dict[str, Any]] | None:
    """Head of the queue, ready or not. A null folder here holds later jobs."""
    pairs = list(iter_queued(root))
    if not pairs:
        return None
    pairs.sort(key=lambda item: _fifo_key(item[1]))
    return pairs[0]


def _oldest_processing(root: Path) -> tuple[Path, dict[str, Any]] | None:
    dirs = ensure_queue_dirs(root)
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in dirs["processing"].glob("*.json"):
        if path.name.endswith(".tmp"):
            continue
        try:
            found.append((path, load_job(path)))
        except (OSError, json.JSONDecodeError, ValueError):
            logger.warning("skipping unreadable processing job %s", path)
    found.sort(key=lambda item: _fifo_key(item[1]))
    return found[0] if found else None


def _count_null_folder(root: Path) -> int:
    waiting = 0
    for _path, job in iter_queued(root):
        if not resolve_folder_path(job):
            waiting += 1
    return waiting


def _latest_by_specialty(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in results:
        slug = record.get("specialty")
        if slug:
            latest[str(slug)] = record
    return latest


def _run_project(path: Path, scripts: SpecialtyScripts, root: Path) -> ProjectRun:
    job = load_job(path)
    job_id = str(job.get("job_id") or path.stem)
    if not resolve_folder_path(job):
        failed = mark_failed(
            job_id,
            "folder_path is null; waiting for ready_for_takeoff (no estimate path invented)",
            queue_root=root,
        )
        return ProjectRun(
            job_id=job_id,
            project_key=failed.get("project_key"),
            status="failed",
            specialties=[],
        )

    order = script_order(job)
    results = [record for record in (job.get("specialty_results") or []) if isinstance(record, dict)]
    for specialty in order:
        latest = _latest_by_specialty(results)
        if latest.get(specialty, {}).get("status") == "ok":
            continue
        started = _utc_now_iso()
        error: str | None = None
        status = "ok"
        logger.info("specialty start job_id=%s specialty=%s", job_id, specialty)
        try:
            scripts.run(specialty, dict(job), job_path=path)
        except Exception as exc:
            status = "failed"
            error = str(exc)[:500]
            logger.warning("specialty failed job_id=%s specialty=%s: %s", job_id, specialty, error)
        results.append(
            {
                "specialty": specialty,
                "status": status,
                "started_at": started,
                "finished_at": _utc_now_iso(),
                "error": error,
            }
        )
        job["specialty_results"] = results
        job["specialties"] = order
        _write_job(path, job)
        logger.info("specialty end job_id=%s specialty=%s status=%s", job_id, specialty, status)

    latest = _latest_by_specialty(results)
    failed_rows = [latest[slug] for slug in order if latest.get(slug, {}).get("status") != "ok"]
    if failed_rows:
        summary = "; ".join(f"{row['specialty']}: {row.get('error') or 'failed'}" for row in failed_rows)
        finished = mark_failed(job_id, summary, queue_root=root)
        status = "failed"
    else:
        finished = mark_done(job_id, queue_root=root)
        status = "done"
    return ProjectRun(
        job_id=job_id,
        project_key=finished.get("project_key"),
        status=status,
        specialties=order,
    )


def _call_module(target: str, specialty: str, job: dict[str, Any]) -> None:
    module_name, sep, func_name = target.partition(":")
    if not sep or not module_name or not func_name:
        raise SpecialtyScriptError(f"module must be 'package.module:function', got {target!r}")
    module = importlib.import_module(module_name)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise SpecialtyScriptError(f"{target} is not callable")
    func(specialty, job)


def _call_command(command: tuple[str, ...], specialty: str, job: dict[str, Any], *, job_path: Path) -> None:
    cm_ids = job.get("cm_ids") if isinstance(job.get("cm_ids"), dict) else {}
    fields = _FormatFields(
        specialty=specialty,
        job_id=str(job.get("job_id") or ""),
        project_key=str(job.get("project_key") or ""),
        folder_path=str(resolve_folder_path(job) or ""),
        project_id=str(cm_ids.get("project_id") or ""),
        estimate_id=str(cm_ids.get("estimate_id") or ""),
    )
    argv = [part.format_map(fields) for part in command]
    if not argv or not argv[0]:
        raise SpecialtyScriptError(f"empty command for {specialty}")
    env = os.environ.copy()
    env["USIS_SPECIALTY"] = specialty
    env["USIS_SPECIALTY_JOB_ID"] = fields["job_id"]
    env["USIS_SPECIALTY_PROJECT_KEY"] = fields["project_key"]
    env["USIS_SPECIALTY_FOLDER_PATH"] = fields["folder_path"]
    env["USIS_SPECIALTY_JOB_FILE"] = str(job_path)
    source_paths = job.get("source_paths") if isinstance(job.get("source_paths"), list) else []
    env["USIS_SPECIALTY_SOURCE_PATHS"] = os.pathsep.join(str(path) for path in source_paths)
    # Blocking. The worker does not start the next script until this returns.
    completed = subprocess.run(argv, check=False, env=env, capture_output=True, text=True)
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-400:]
        raise SpecialtyScriptError(f"exit {completed.returncode}: {tail}".strip())


def _parse_spec(specialty: str, raw: Any) -> ScriptSpec:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError(f"{specialty}: empty runner")
        if ":" in text and " " not in text:
            return ScriptSpec(specialty=specialty, module=text)
        return ScriptSpec(specialty=specialty, command=tuple(shlex.split(text, posix=os.name != "nt")))
    if isinstance(raw, list):
        if not raw:
            raise ValueError(f"{specialty}: empty command")
        return ScriptSpec(specialty=specialty, command=tuple(str(part) for part in raw))
    if isinstance(raw, dict):
        module = raw.get("module")
        command = raw.get("command")
        if module and command:
            raise ValueError(f"{specialty}: set module or command, not both")
        if module:
            return ScriptSpec(specialty=specialty, module=str(module).strip())
        if isinstance(command, str):
            return ScriptSpec(
                specialty=specialty,
                command=tuple(shlex.split(command, posix=os.name != "nt")),
            )
        if isinstance(command, list) and command:
            return ScriptSpec(specialty=specialty, command=tuple(str(part) for part in command))
        raise ValueError(f"{specialty}: missing module or command")
    raise ValueError(f"{specialty}: runner must be a command or module")


def describe_runners(specs: dict[str, ScriptSpec]) -> str:
    parts = []
    for slug in ALL_SPECIALTIES:
        spec = specs.get(slug)
        if spec is None:
            kind = "missing"
        elif spec.module:
            kind = "module"
        else:
            kind = "command"
        parts.append(f"{slug}={kind}")
    return ", ".join(parts)


def load_runners_file(path: Path | None) -> dict[str, ScriptSpec]:
    """Load runners and log which of the nine slugs are plugged in."""
    specs = load_script_specs(path)
    if specs:
        logger.info("specialty runners: %s", describe_runners(specs))
    return specs


__all__ = [
    "RUNNER_ID",
    "RUNNERS_ENV",
    "ConfiguredScripts",
    "ProjectRun",
    "RunReport",
    "ScriptSpec",
    "SpecialtyRunnerBusy",
    "SpecialtyScriptError",
    "SpecialtyScripts",
    "describe_runners",
    "load_script_specs",
    "missing_runners",
    "run_queue",
    "runner_lock",
    "script_order",
]
