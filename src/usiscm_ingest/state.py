"""Per-file ingest state so ACC folders can receive addenda over time."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from usiscm_ingest.classify import ClassifiedFile
from usiscm_ingest.package import PackageManifest

logger = logging.getLogger(__name__)


def posix_key(relative_path: str) -> str:
    return relative_path.replace("\\", "/")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class FileRecord:
    relative_path: str
    size: int
    mtime_ns: int
    sha256: str
    category: str
    status: str
    ingested_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "relative_path": self.relative_path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "category": self.category,
            "status": self.status,
            "ingested_at": self.ingested_at,
        }
        if self.error:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FileRecord:
        return cls(
            relative_path=posix_key(str(raw.get("relative_path") or "")),
            size=int(raw.get("size") or 0),
            mtime_ns=int(raw.get("mtime_ns") or 0),
            sha256=str(raw.get("sha256") or ""),
            category=str(raw.get("category") or ""),
            status=str(raw.get("status") or "imported"),
            ingested_at=raw.get("ingested_at"),
            error=raw.get("error"),
        )


@dataclass
class FileDelta:
    item: ClassifiedFile
    reason: str  # new | changed | retry | reprocess
    digest: str
    size: int
    mtime_ns: int


@dataclass
class ScanResult:
    pending: list[FileDelta] = field(default_factory=list)
    unchanged: int = 0
    settling: int = 0

    @property
    def items(self) -> list[ClassifiedFile]:
        return [delta.item for delta in self.pending]


@dataclass
class PackageState:
    source: str
    label: str
    files: dict[str, FileRecord] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    project_id: int | None = None
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "project_id": self.project_id,
            "files": {key: record.to_dict() for key, record in sorted(self.files.items())},
            "history": self.history[-50:],
        }

    def save(self, dest: Path | None = None) -> Path:
        dest = dest or self.path
        if dest is None:
            raise ValueError("No path to save package state")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        self.path = dest
        return dest

    @classmethod
    def load(cls, path: Path, *, source: str, label: str) -> PackageState:
        if not path.exists():
            return cls(source=source, label=label, path=path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        files = {
            posix_key(key): FileRecord.from_dict(value)
            for key, value in (raw.get("files") or {}).items()
            if isinstance(value, dict)
        }
        return cls(
            source=str(raw.get("source") or source),
            label=str(raw.get("label") or label),
            files=files,
            history=list(raw.get("history") or []),
            project_id=raw.get("project_id"),
            path=path,
        )

    def mark_imported(self, deltas: Iterable[FileDelta], *, project_id: int | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        added: list[str] = []
        changed: list[str] = []
        for delta in deltas:
            key = posix_key(delta.item.relative_path)
            previous = self.files.get(key)
            self.files[key] = FileRecord(
                relative_path=key,
                size=delta.size,
                mtime_ns=delta.mtime_ns,
                sha256=delta.digest,
                category=delta.item.category.value,
                status="imported",
                ingested_at=now,
            )
            if previous is None:
                added.append(key)
            else:
                changed.append(key)
        if project_id is not None:
            self.project_id = project_id
        self.history.append(
            {
                "at": now,
                "added": added,
                "changed": changed,
                "imported": len(added) + len(changed),
            }
        )

    def mark_failed(self, deltas: Iterable[FileDelta], error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        failed: list[str] = []
        for delta in deltas:
            key = posix_key(delta.item.relative_path)
            failed.append(key)
            self.files[key] = FileRecord(
                relative_path=key,
                size=delta.size,
                mtime_ns=delta.mtime_ns,
                sha256=delta.digest,
                category=delta.item.category.value,
                status="failed",
                ingested_at=now,
                error=error,
            )
        self.history.append({"at": now, "failed": failed, "error": error})


def scan_changes(
    manifest: PackageManifest,
    state: PackageState,
    *,
    reprocess: bool = False,
    settle_seconds: int = 0,
) -> ScanResult:
    """Return files that are new, replaced, previously failed, or still copying."""
    result = ScanResult()
    now_ns = time.time_ns()
    settle_ns = max(settle_seconds, 0) * 1_000_000_000
    for item in manifest.files:
        try:
            stat = item.path.stat()
        except OSError:
            continue
        if settle_ns and (now_ns - stat.st_mtime_ns) < settle_ns:
            result.settling += 1
            continue
        key = posix_key(item.relative_path)
        previous = state.files.get(key)
        if reprocess:
            result.pending.append(_delta(item, stat, "reprocess"))
            continue
        if previous is None:
            result.pending.append(_delta(item, stat, "new"))
            continue
        if previous.status == "failed":
            result.pending.append(_delta(item, stat, "retry"))
            continue
        if previous.size == stat.st_size and previous.mtime_ns == stat.st_mtime_ns:
            result.unchanged += 1
            continue
        digest = file_digest(item.path)
        if previous.sha256 and digest == previous.sha256:
            previous.size = stat.st_size
            previous.mtime_ns = stat.st_mtime_ns
            result.unchanged += 1
            continue
        result.pending.append(
            FileDelta(
                item=item,
                reason="changed",
                digest=digest,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    return result


def seed_from_legacy_manifest(
    state: PackageState,
    manifest: PackageManifest,
    legacy_path: Path,
) -> None:
    """Treat files listed in a previous whole-package sidecar as already imported."""
    if state.files or not legacy_path.exists():
        return
    try:
        raw = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    listed = {
        posix_key(str(item.get("relative_path") or ""))
        for item in (raw.get("files") or [])
        if isinstance(item, dict)
    }
    listed.discard("")
    if not listed:
        return
    now = datetime.now(timezone.utc).isoformat()
    for item in manifest.files:
        key = posix_key(item.relative_path)
        if key not in listed:
            continue
        try:
            stat = item.path.stat()
        except OSError:
            continue
        state.files[key] = FileRecord(
            relative_path=key,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sha256=file_digest(item.path),
            category=item.category.value,
            status="imported",
            ingested_at=now,
        )
    logger.info("Migrated %d previously ingested file(s) from %s", len(state.files), legacy_path.name)


def pending_manifest(manifest: PackageManifest, deltas: Iterable[FileDelta]) -> PackageManifest:
    files = [delta.item for delta in deltas]
    return PackageManifest(
        source=manifest.source,
        root_dir=manifest.root_dir,
        label=manifest.label,
        files=files,
        errors=list(manifest.errors),
    )


def _delta(item: ClassifiedFile, stat: Any, reason: str) -> FileDelta:
    return FileDelta(
        item=item,
        reason=reason,
        digest=file_digest(item.path),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )
