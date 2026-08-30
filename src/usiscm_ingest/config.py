"""Environment and optional YAML hint overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from usiscm_ingest.classify import DEFAULT_HINTS, CategoryHints, FileCategory

load_dotenv()


@dataclass
class Settings:
    base_url: str
    email: str
    password: str
    watch_dir: Path | None = None
    work_dir: Path | None = None
    processed_dir: Path | None = None
    failed_dir: Path | None = None
    poll_seconds: int = 60


def load_settings() -> Settings:
    watch = os.getenv("USISCM_WATCH_DIR", "").strip()
    work = os.getenv("USISCM_WORK_DIR", "").strip()
    processed = os.getenv("USISCM_PROCESSED_DIR", "").strip()
    failed = os.getenv("USISCM_FAILED_DIR", "").strip()
    return Settings(
        base_url=os.getenv("USISCM_BASE_URL", "https://www.usiscm.com").rstrip("/"),
        email=os.getenv("USISCM_EMAIL", "").strip(),
        password=os.getenv("USISCM_PASSWORD", "").strip(),
        watch_dir=Path(watch).expanduser() if watch else None,
        work_dir=Path(work).expanduser() if work else None,
        processed_dir=Path(processed).expanduser() if processed else None,
        failed_dir=Path(failed).expanduser() if failed else None,
        poll_seconds=int(os.getenv("USISCM_POLL_SECONDS", "60")),
    )


def load_hints(config_path: Path | None) -> dict[FileCategory, CategoryHints]:
    """Merge optional YAML overrides on top of built-in hints."""
    hints = dict(DEFAULT_HINTS)
    if config_path is None or not config_path.exists():
        return hints

    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load a hints config file") from exc

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    override = raw.get("hints") or raw
    if not isinstance(override, dict):
        return hints

    for key, value in override.items():
        try:
            category = FileCategory(key)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        base = hints[category]
        hints[category] = CategoryHints(
            folders=tuple(value.get("folders", base.folders)),
            filenames=tuple(value.get("filenames", base.filenames)),
            text=tuple(value.get("text", base.text)),
        )
    return hints
