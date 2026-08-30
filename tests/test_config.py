from pathlib import Path

from usiscm_ingest.classify import FileCategory
from usiscm_ingest.config import DEFAULT_FAILED_DIR, DEFAULT_PROCESSED_DIR, DEFAULT_WATCH_DIR, load_hints, load_settings


def test_yaml_overrides_only_listed_categories(tmp_path: Path) -> None:
    path = tmp_path / "hints.yaml"
    path.write_text(
        """
hints:
  drawing:
    folders: [planroom]
    filenames: [sheet]
    text: []
""",
        encoding="utf-8",
    )
    hints = load_hints(path)
    assert hints[FileCategory.DRAWING].folders == ("planroom",)
    assert "spec" in hints[FileCategory.SPEC].folders


def test_default_watch_dir_is_accdocs(monkeypatch) -> None:
    monkeypatch.delenv("USISCM_WATCH_DIR", raising=False)
    monkeypatch.delenv("USISCM_PROCESSED_DIR", raising=False)
    monkeypatch.delenv("USISCM_FAILED_DIR", raising=False)
    monkeypatch.delenv("USISCM_LEAVE_IN_PLACE", raising=False)
    settings = load_settings()
    assert settings.watch_dir == DEFAULT_WATCH_DIR
    assert settings.processed_dir == DEFAULT_PROCESSED_DIR
    assert settings.failed_dir == DEFAULT_FAILED_DIR
    assert settings.leave_in_place is True


def test_empty_watch_dir_env_disables_default(monkeypatch) -> None:
    monkeypatch.setenv("USISCM_WATCH_DIR", "")
    assert load_settings().watch_dir is None
