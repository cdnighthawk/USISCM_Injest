"""Load a zip or folder as an estimate package and classify every file.

Package identity comes from the zip/folder the operator points at — not from
any GC-specific naming convention such as ``Progress Print``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from usiscm_ingest.classify import (
    ClassifiedFile,
    FileCategory,
    classify_files,
    should_skip,
)

logger = logging.getLogger(__name__)

_REVISION_SUFFIX_RE = re.compile(
    r"""
    (?:
        [\s_\-]*progress[\s_\-]*print
        |[\s_\-]*rev(?:ision)?[\s_\-]*\w+
        |[\s_\-]*v(?:er(?:sion)?)?[\s_\-]*\d+
        |[\s_\-]+\d+
    )+$
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class PackageManifest:
    source: Path
    root_dir: Path
    label: str
    files: list[ClassifiedFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        totals = {category.value: 0 for category in FileCategory}
        for item in self.files:
            totals[item.category.value] += 1
        return totals

    def files_for(self, category: FileCategory) -> list[ClassifiedFile]:
        return [item for item in self.files if item.category == category]

    def to_dict(self) -> dict:
        return {
            "source": str(self.source),
            "root_dir": str(self.root_dir),
            "label": self.label,
            "counts": self.counts,
            "errors": self.errors,
            "files": [item.to_dict() for item in self.files],
        }

    def write_json(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return dest


def package_label(source: Path) -> str:
    """Human label for matching a USISCM project.

    Strips common *optional* suffixes (revision numbers, "progress print") so
    a Turner-style zip still resolves to a project name. Those suffixes are
    never required for ingest.
    """
    stem = source.stem if source.suffix.lower() == ".zip" else source.name
    cleaned = _REVISION_SUFFIX_RE.sub("", stem).strip(" ._-\t")
    return cleaned or stem


def collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not should_skip(path):
            files.append(path)
    return files


def extract_zip(zip_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            member_path = Path(info.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                logger.warning("Skipping unsafe zip member %s", info.filename)
                continue
            archive.extract(info, dest_dir)
    return dest_dir


def ingest_source(
    source: Path,
    work_dir: Path | None = None,
    *,
    peek_pdf: bool = False,
) -> PackageManifest:
    """Ingest a zip archive or an already-extracted folder."""
    source = source.expanduser().resolve()
    errors: list[str] = []

    if not source.exists():
        return PackageManifest(
            source=source,
            root_dir=source,
            label=package_label(source),
            errors=[f"Source does not exist: {source}"],
        )

    if source.suffix.lower() == ".zip":
        if work_dir is None:
            work_dir = source.parent / ".usiscm-ingest-work"
        target = work_dir / _safe_stem(source.stem)
        if target.exists():
            shutil.rmtree(target)
        try:
            root = extract_zip(source, target)
        except zipfile.BadZipFile as exc:
            return PackageManifest(
                source=source,
                root_dir=target,
                label=package_label(source),
                errors=[f"Bad zip: {exc}"],
            )
    elif source.is_dir():
        root = source
    else:
        return PackageManifest(
            source=source,
            root_dir=source.parent,
            label=package_label(source),
            errors=[f"Source must be a .zip or a directory: {source}"],
        )

    # If the archive contains a single top-level folder, classify from there
    # so relative paths do not include a redundant wrapper directory.
    children = [p for p in root.iterdir() if not should_skip(p)]
    if len(children) == 1 and children[0].is_dir():
        classify_root = children[0]
    else:
        classify_root = root

    files = classify_files(collect_files(classify_root), root=classify_root, peek_pdf=peek_pdf)
    logger.info(
        "Ingested %s (%d files): %s",
        source.name,
        len(files),
        ", ".join(f"{k}={v}" for k, v in PackageManifest(source, root, package_label(source), files).counts.items() if v),
    )
    return PackageManifest(
        source=source,
        root_dir=classify_root,
        label=package_label(source),
        files=files,
        errors=errors,
    )


def iter_packages(source_dir: Path) -> list[Path]:
    """Immediate zips and subfolders in a drop directory (not recursive)."""
    source_dir = source_dir.expanduser().resolve()
    packages: list[Path] = []
    for path in sorted(source_dir.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_dir() or path.suffix.lower() == ".zip":
            packages.append(path)
    return packages


def stamp_name(path: Path) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{now}_{path.name}"


def _safe_stem(stem: str) -> str:
    cleaned = re.sub(r"[^\w.\- ]+", "_", stem).strip()
    return cleaned[:80] or "package"
