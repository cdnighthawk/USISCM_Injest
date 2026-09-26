"""Keep specialty-takeoff jobs out of the default Windows queue during tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def specialty_takeoff_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "specialty-takeoff"
    monkeypatch.setenv("USIS_SPECIALTY_TAKEOFF_QUEUE", str(root))
    return root
