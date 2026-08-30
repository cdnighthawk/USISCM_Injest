from pathlib import Path

from usiscm_ingest.classify import FileCategory
from usiscm_ingest.config import load_hints


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
