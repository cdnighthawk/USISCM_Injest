"""Specialty takeoff queue: enqueue (this app) and optional claim (bots).

Schema ``usis.specialty_takeoff.v1``. One JSON file per job:

    C:\\usis-cm\\data\\queues\\specialty_takeoff\\queued\\{job_id}.json

Override the root with ``USIS_SPECIALTY_TAKEOFF_QUEUE``.

Enqueue runs after a clean ``UsiscmClient.import_package`` batch. It never
raises into the watcher or upload path. ``folder_path`` may be null; this
module does not invent an estimate-folder location. A later
``patch_job_folder_path`` sets the path and ``status=ready_for_takeoff``.

Claim, run, and finish belong to specialty bots. The tray does not drive
this queue.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SCHEMA_ID = "usis.specialty_takeoff.v1"
SOURCE = "usiscm_ingest"
QUEUE_ENV = "USIS_SPECIALTY_TAKEOFF_QUEUE"
DEFAULT_QUEUE_ROOT = Path(r"C:\usis-cm\data\queues\specialty_takeoff")
STAGES = ("queued", "processing", "done", "failed")

# Nine specialty slugs. Enqueue may store ["all"]; claim expands that.
ALL_SPECIALTIES = (
    "lockers",
    "concrete",
    "door_spec",
    "room_interiors",
    "wall_protection",
    "partitions",
    "fec",
    "millwork",
    "bathroom_accessories",
)

READY_STATUSES = frozenset({"queued", "ready_for_takeoff"})


class SpecialtyTakeoffNotReady(Exception):
    """Queued job has no folder_path yet. Leave it for a later patch."""


def queue_root() -> Path:
    """Queue root from ``USIS_SPECIALTY_TAKEOFF_QUEUE`` or the Windows default."""
    return Path(os.environ.get(QUEUE_ENV, str(DEFAULT_QUEUE_ROOT)))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nonempty(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def ensure_queue_dirs(root: Path | None = None) -> dict[str, Path]:
    """Create ``queued``, ``processing``, ``done``, and ``failed`` under ``root``."""
    base = Path(root) if root is not None else queue_root()
    dirs = {name: base / name for name in STAGES}
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    return dirs


def _write_job(path: Path, job: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(job, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _normalize_cm_ids(
    cm_ids: dict[str, Any] | list[Any] | tuple[Any, ...] | None,
    *,
    project_id: str | None,
    estimate_id: str | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(cm_ids, dict):
        out.update(cm_ids)
    if project_id is not None and "project_id" not in out:
        out["project_id"] = project_id
    if estimate_id is not None and "estimate_id" not in out:
        out["estimate_id"] = estimate_id
    out.setdefault("project_id", None)
    out.setdefault("estimate_id", None)
    return out


def _normalize_specialties(specialties: list[str] | tuple[str, ...] | None) -> list[str]:
    """Store ``["all"]`` or concrete slugs. Expansion happens at claim time."""
    if not specialties:
        return ["all"]
    specs = [str(item).strip() for item in specialties if _nonempty(item)]
    if not specs or any(item.lower() == "all" for item in specs):
        return ["all"]
    return specs


def _normalize_ids(file_ids: list[Any] | tuple[Any, ...] | None) -> list[str]:
    ids: list[str] = []
    for item in file_ids or []:
        text = _nonempty(item)
        if text:
            ids.append(text)
    return ids


def enqueue_specialty_takeoff(
    *,
    project_key: str | None = None,
    project_id: str | None = None,
    estimate_id: str | None = None,
    folder_path: str | None = None,
    cm_ids: dict[str, Any] | list[Any] | tuple[Any, ...] | None = None,
    source_paths: list[str] | tuple[str, ...] | None = None,
    sheet_paths: list[str] | tuple[str, ...] | None = None,
    specialties: list[str] | tuple[str, ...] | None = None,
    trigger: str = "ingest_ok",
    file_ids: list[Any] | tuple[Any, ...] | None = None,
    extra: dict[str, Any] | None = None,
    root: Path | None = None,
) -> Path | None:
    """Write ``queued/{job_id}.json``. Returns the path, or None on failure.

    Never raises. ``folder_path`` may be null; callers must not invent a
    drive path. Per-job JSON is the contract (no ``queue.jsonl``).
    """
    try:
        dirs = ensure_queue_dirs(root)
        job_id = str(uuid.uuid4())
        paths = [str(path) for path in (source_paths if source_paths is not None else sheet_paths or []) if path]
        folder = _nonempty(folder_path)
        payload: dict[str, Any] = {
            "schema": SCHEMA_ID,
            "job_id": job_id,
            "project_key": _nonempty(project_key),
            "cm_ids": _normalize_cm_ids(cm_ids, project_id=_nonempty(project_id), estimate_id=_nonempty(estimate_id)),
            "source_paths": paths,
            "specialties": _normalize_specialties(specialties),
            "folder_path": folder,
            "estimate_folder": folder,
            "enqueued_at": _utc_now_iso(),
            "status": "queued",
            "trigger": _nonempty(trigger) or "ingest_ok",
            "file_ids": _normalize_ids(file_ids),
            "error": None,
            "claimed_at": None,
            "claimed_by": None,
            "finished_at": None,
            "source": SOURCE,
        }
        if extra:
            for key, value in extra.items():
                if key not in payload:
                    payload[key] = value
        path = dirs["queued"] / f"{job_id}.json"
        _write_job(path, payload)
        logger.info(
            "specialty takeoff queued job_id=%s paths=%d folder=%s",
            job_id,
            len(paths),
            folder or "(null)",
        )
        return path
    except Exception as exc:
        logger.warning("specialty takeoff enqueue failed: %s", exc)
        return None


def patch_job_folder_path(
    job_path: str | Path,
    folder_path: str,
    *,
    status: str = "ready_for_takeoff",
) -> bool:
    """Fill ``folder_path`` later. Preferred status is ``ready_for_takeoff``.

    Does not move the file. Returns False on failure and never raises.
    """
    try:
        folder = _nonempty(folder_path)
        if not folder:
            logger.warning("patch_job_folder_path missing folder_path")
            return False
        path = Path(job_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        data["folder_path"] = folder
        data["estimate_folder"] = folder
        data["folder_path_patched_at"] = _utc_now_iso()
        if status:
            data["status"] = status
        _write_job(path, data)
        return True
    except Exception as exc:
        logger.warning("patch_job_folder_path failed: %s", exc)
        return False


def expand_specialties(specialties: list[Any] | tuple[Any, ...] | None) -> list[str]:
    """Return concrete slugs. Missing or ``all`` becomes the nine specialties."""
    if not specialties:
        return list(ALL_SPECIALTIES)
    specs = [str(item).strip() for item in specialties if _nonempty(item)]
    if not specs or any(item.lower() == "all" for item in specs):
        return list(ALL_SPECIALTIES)
    return specs


def resolve_folder_path(job: dict[str, Any] | None) -> str | None:
    """Prefer ``folder_path``. ``estimate_folder`` is the deprecated alias."""
    if not isinstance(job, dict):
        return None
    direct = _nonempty(job.get("folder_path"))
    if direct:
        return direct
    return _nonempty(job.get("estimate_folder"))


def normalize_job_fields(job: dict[str, Any]) -> dict[str, Any]:
    """Copy with specialties expanded and ``folder_path`` promoted from the alias."""
    normalized = dict(job)
    normalized["specialties"] = expand_specialties(job.get("specialties"))
    folder = resolve_folder_path(job)
    normalized["folder_path"] = folder
    normalized["estimate_folder"] = folder
    return normalized


def is_ready(job: dict[str, Any]) -> bool:
    """True when a consumer may claim: folder path set, still waiting to run."""
    if not resolve_folder_path(job):
        return False
    status = str(job.get("status") or "queued")
    return status in READY_STATUSES


def load_job(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"job is not an object: {path}")
    return data


def _job_path(stage_dir: Path, job_id: str) -> Path:
    return stage_dir / f"{job_id}.json"


def iter_queued(queue_root: Path | None = None) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield ``(path, job)`` for every ``*.json`` currently in ``queued/``."""
    dirs = ensure_queue_dirs(queue_root)
    for path in sorted(dirs["queued"].glob("*.json")):
        if path.name.endswith(".tmp"):
            continue
        try:
            job = load_job(path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        yield path, job


def iter_ready(queue_root: Path | None = None) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield queued jobs whose ``folder_path`` is set.

    Jobs enqueued with a null path stay in ``queued/`` until patched.
    """
    for path, job in iter_queued(queue_root):
        if is_ready(job):
            yield path, job


def claim_job(
    job_id: str,
    *,
    claimed_by: str,
    queue_root: Path | None = None,
) -> dict[str, Any]:
    """Move ``queued/{job_id}.json`` to ``processing/`` and mark it claimed.

    Refuses a job that still has no ``folder_path`` (file stays in ``queued/``).
    Expands ``specialties=["all"]`` on the claimed copy.
    """
    dirs = ensure_queue_dirs(queue_root)
    src = _job_path(dirs["queued"], job_id)
    dst = _job_path(dirs["processing"], job_id)
    if not src.is_file():
        raise FileNotFoundError(f"no queued job: {src}")
    if dst.exists():
        raise FileExistsError(f"already processing: {dst}")

    job = normalize_job_fields(load_job(src))
    if not job.get("folder_path"):
        raise SpecialtyTakeoffNotReady(
            f"job {job_id} has no folder_path; waiting for ready_for_takeoff"
        )
    job["status"] = "processing"
    job["claimed_at"] = _utc_now_iso()
    job["claimed_by"] = claimed_by
    job.setdefault("schema", SCHEMA_ID)
    _write_job(dst, job)
    try:
        src.unlink()
    except FileNotFoundError as exc:
        raise FileExistsError(f"race: queued job vanished during claim: {job_id}") from exc
    return job


def mark_done(
    job_id: str,
    *,
    queue_root: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Move ``processing/{job_id}.json`` to ``done/`` with ``status=done``."""
    return _finish(job_id, stage="done", queue_root=queue_root, extra=extra, error=None)


def mark_failed(
    job_id: str,
    error: str,
    *,
    queue_root: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Move ``processing/{job_id}.json`` to ``failed/`` with ``status=failed``."""
    return _finish(job_id, stage="failed", queue_root=queue_root, extra=extra, error=error)


def _finish(
    job_id: str,
    *,
    stage: str,
    queue_root: Path | None,
    extra: dict[str, Any] | None,
    error: str | None,
) -> dict[str, Any]:
    dirs = ensure_queue_dirs(queue_root)
    src = _job_path(dirs["processing"], job_id)
    dst = _job_path(dirs[stage], job_id)
    if not src.is_file():
        raise FileNotFoundError(f"not in processing: {src}")
    job = load_job(src)
    job["status"] = stage
    job["finished_at"] = _utc_now_iso()
    job["error"] = error
    if extra:
        job.update(extra)
    job.setdefault("schema", SCHEMA_ID)
    _write_job(dst, job)
    src.unlink(missing_ok=True)
    return job


__all__ = [
    "ALL_SPECIALTIES",
    "DEFAULT_QUEUE_ROOT",
    "QUEUE_ENV",
    "SCHEMA_ID",
    "SOURCE",
    "SpecialtyTakeoffNotReady",
    "claim_job",
    "enqueue_specialty_takeoff",
    "ensure_queue_dirs",
    "expand_specialties",
    "is_ready",
    "iter_queued",
    "iter_ready",
    "load_job",
    "mark_done",
    "mark_failed",
    "normalize_job_fields",
    "patch_job_folder_path",
    "queue_root",
    "resolve_folder_path",
]
