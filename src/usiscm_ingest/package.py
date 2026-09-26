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

    classify_root = root
    # Zip archives often wrap contents in one folder; peel that so relative
    # paths match the inner set. Live ACC project folders must keep a stable
    # root or adding Addenda would rewrite every existing path and re-upload.
    if source.suffix.lower() == ".zip":
        children = [p for p in root.iterdir() if not should_skip(p)]
        if len(children) == 1 and children[0].is_dir():
            classify_root = children[0]

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


_SKIP_DIR_NAMES = {"processed", "failed"}


def _visible_children(path: Path) -> list[Path]:
    return sorted(
        child
        for child in path.iterdir()
        if not child.name.startswith(".") and child.name not in _SKIP_DIR_NAMES
    )


def _looks_like_acc_hub(path: Path) -> bool:
    """True when a folder is an ACC account/hub (projects nested one level down)."""
    if not path.is_dir():
        return False
    children = _visible_children(path)
    return bool(children) and all(child.is_dir() for child in children)


def package_matches(package: Path, needles: list[str] | None) -> bool:
    """True when ``package`` is one of the requested jobs (for example ``26092``)."""
    if not needles:
        return True
    name = package.name.lower()
    parts = [part.lower() for part in package.parts]
    blob = str(package).replace("\\", "/").lower()
    for needle in needles:
        n = needle.strip().lower()
        if not n:
            continue
        if n == name or n in parts or n in name or f"/{n}/" in f"/{blob}/" or blob.endswith("/" + n):
            return True
    return False


def package_state_key(drop: Path, package: Path) -> str:
    """Stable sidecar filename for a package relative to the ACCDocs root."""
    try:
        rel = package.resolve().relative_to(drop.resolve())
    except ValueError:
        rel = Path(package.name)
    return "__".join(rel.parts) or package.name


def iter_packages(source_dir: Path) -> list[Path]:
    """Zips and project folders under a drop directory.

    Autodesk Desktop Connector lays ACC out as ``ACCDocs/<hub>/<project>/...``.
    Those hub folders are expanded so each ACC project is imported separately.
    A flat drop of zips or estimate folders still works the same as before.
    """
    source_dir = source_dir.expanduser().resolve()
    packages: list[Path] = []
    for path in _visible_children(source_dir):
        if path.suffix.lower() == ".zip" and path.is_file():
            packages.append(path)
            continue
        if not path.is_dir():
            continue
        if _looks_like_acc_hub(path):
            packages.extend(child for child in _visible_children(path) if child.is_dir())
        else:
            packages.append(path)
    return packages


def stamp_name(path: Path) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{now}_{path.name}"


def _safe_stem(stem: str) -> str:
    cleaned = re.sub(r"[^\w.\- ]+", "_", stem).strip()
    return cleaned[:80] or "package"
