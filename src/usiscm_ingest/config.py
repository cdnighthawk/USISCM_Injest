"""Environment and optional YAML hint overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from usiscm_ingest.classify import DEFAULT_HINTS, CategoryHints, FileCategory
from usiscm_ingest.microsoft import DEFAULT_TOKEN_PATH

load_dotenv()

# Autodesk Desktop Connector sync root on this server.
DEFAULT_WATCH_DIR = Path(r"C:\Users\CharlesDossett\DC\ACCDocs")
DEFAULT_PROCESSED_DIR = Path(r"C:\Users\CharlesDossett\DC\USISCM-ingest\processed")
DEFAULT_FAILED_DIR = Path(r"C:\Users\CharlesDossett\DC\USISCM-ingest\failed")


@dataclass
class Settings:
    base_url: str
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_access_token: str = ""
    token_path: Path = DEFAULT_TOKEN_PATH
    ingest_api_key: str = ""
    watch_dir: Path | None = None
    work_dir: Path | None = None
    processed_dir: Path | None = None
    failed_dir: Path | None = None
    poll_seconds: int = 60
    settle_seconds: int = 15
    leave_in_place: bool = True
    sheet_ai: bool = True
    upload_timeout: int = 600
    specialty_runners_path: Path | None = None


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
    token_path = os.getenv("USISCM_MS_TOKEN_PATH", "").strip()
    return Settings(
        base_url=os.getenv("USISCM_BASE_URL", "https://www.usiscm.com").rstrip("/"),
        ms_tenant_id=os.getenv("USISCM_MS_TENANT_ID", "").strip(),
        ms_client_id=os.getenv("USISCM_MS_CLIENT_ID", "").strip(),
        ms_access_token=os.getenv("USISCM_MS_ACCESS_TOKEN", "").strip(),
        token_path=Path(token_path).expanduser() if token_path else DEFAULT_TOKEN_PATH,
        ingest_api_key=os.getenv("USISCM_INGEST_API_KEY", "").strip(),
        watch_dir=_path_env("USISCM_WATCH_DIR", DEFAULT_WATCH_DIR),
        work_dir=_path_env("USISCM_WORK_DIR"),
        processed_dir=_path_env("USISCM_PROCESSED_DIR", DEFAULT_PROCESSED_DIR),
        failed_dir=_path_env("USISCM_FAILED_DIR", DEFAULT_FAILED_DIR),
        poll_seconds=int(os.getenv("USISCM_POLL_SECONDS", "60")),
        settle_seconds=int(os.getenv("USISCM_SETTLE_SECONDS", "15")),
        leave_in_place=_bool_env("USISCM_LEAVE_IN_PLACE", True),
        sheet_ai=_bool_env("USISCM_SHEET_AI", True),
        upload_timeout=int(os.getenv("USISCM_UPLOAD_TIMEOUT", "600")),
        specialty_runners_path=_path_env("USIS_SPECIALTY_RUNNERS"),
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
