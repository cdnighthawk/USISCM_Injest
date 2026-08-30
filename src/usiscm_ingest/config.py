"""Environment and optional YAML hint overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from usiscm_ingest.classify import DEFAULT_HINTS, CategoryHints, FileCategory

load_dotenv()

# Autodesk Desktop Connector sync root on this server.
DEFAULT_WATCH_DIR = Path(r"C:\Users\CharlesDossett\DC\ACCDocs")
DEFAULT_PROCESSED_DIR = Path(r"C:\Users\CharlesDossett\DC\USISCM-ingest\processed")
DEFAULT_FAILED_DIR = Path(r"C:\Users\CharlesDossett\DC\USISCM-ingest\failed")


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
    leave_in_place: bool = True


def _path_env(name: str, default: Path | None = None) -> Path | None:
    if name not in os.environ:
        return default
    raw = os.environ[name].strip()
    return Path(raw).expanduser() if raw else None


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    return Settings(
        base_url=os.getenv("USISCM_BASE_URL", "https://www.usiscm.com").rstrip("/"),
        email=os.getenv("USISCM_EMAIL", "").strip(),
        password=os.getenv("USISCM_PASSWORD", "").strip(),
        watch_dir=_path_env("USISCM_WATCH_DIR", DEFAULT_WATCH_DIR),
        work_dir=_path_env("USISCM_WORK_DIR"),
        processed_dir=_path_env("USISCM_PROCESSED_DIR", DEFAULT_PROCESSED_DIR),
        failed_dir=_path_env("USISCM_FAILED_DIR", DEFAULT_FAILED_DIR),
        poll_seconds=int(os.getenv("USISCM_POLL_SECONDS", "60")),
        leave_in_place=_bool_env("USISCM_LEAVE_IN_PLACE", True),
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
