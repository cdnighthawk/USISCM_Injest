"""Environment and optional YAML hint overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from usiscm_ingest.classify import DEFAULT_HINTS, CategoryHints, FileCategory
from usiscm_ingest.microsoft import DEFAULT_TOKEN_PATH

load_dotenv()


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


def load_settings() -> Settings:
    watch = os.getenv("USISCM_WATCH_DIR", "").strip()
    work = os.getenv("USISCM_WORK_DIR", "").strip()
    processed = os.getenv("USISCM_PROCESSED_DIR", "").strip()
    failed = os.getenv("USISCM_FAILED_DIR", "").strip()
    token_path = os.getenv("USISCM_MS_TOKEN_PATH", "").strip()
    return Settings(
        base_url=os.getenv("USISCM_BASE_URL", "https://www.usiscm.com").rstrip("/"),
        ms_tenant_id=os.getenv("USISCM_MS_TENANT_ID", "").strip(),
        ms_client_id=os.getenv("USISCM_MS_CLIENT_ID", "").strip(),
        ms_access_token=os.getenv("USISCM_MS_ACCESS_TOKEN", "").strip(),
        token_path=Path(token_path).expanduser() if token_path else DEFAULT_TOKEN_PATH,
        ingest_api_key=os.getenv("USISCM_INGEST_API_KEY", "").strip(),
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
