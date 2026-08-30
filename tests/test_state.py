from pathlib import Path

from usiscm_ingest.package import ingest_source
from usiscm_ingest.state import PackageState, scan_changes, seed_from_legacy_manifest


def _write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_scan_detects_new_addendum(tmp_path: Path) -> None:
    root = tmp_path / "Kaiser MOB"
    _write(root / "Drawings" / "A-101.pdf")
    manifest = ingest_source(root)
    state = PackageState(source=str(root), label=manifest.label)
    first = scan_changes(manifest, state, settle_seconds=0)
    assert {delta.reason for delta in first.pending} == {"new"}
    state.mark_imported(first.pending)

    _write(root / "Addenda" / "Addendum 02.pdf", b"addendum")
    again = ingest_source(root)
    second = scan_changes(again, state, settle_seconds=0)
    assert len(second.pending) == 1
    assert second.pending[0].reason == "new"
    assert second.pending[0].item.path.name == "Addendum 02.pdf"
    assert second.unchanged == 1


def test_scan_detects_replaced_file(tmp_path: Path) -> None:
    root = tmp_path / "job"
    target = root / "Addenda" / "Addendum 01.pdf"
    _write(target, b"original")
    manifest = ingest_source(root)
    state = PackageState(source=str(root), label=manifest.label)
    state.mark_imported(scan_changes(manifest, state, settle_seconds=0).pending)

    _write(target, b"revised addendum bytes")
    changed = scan_changes(ingest_source(root), state, settle_seconds=0)
    assert len(changed.pending) == 1
    assert changed.pending[0].reason == "changed"


def test_scan_ignores_mtime_only_touch(tmp_path: Path) -> None:
    root = tmp_path / "job"
    target = root / "A-101.pdf"
    _write(target, b"same-bytes")
    manifest = ingest_source(root)
    state = PackageState(source=str(root), label=manifest.label)
    state.mark_imported(scan_changes(manifest, state, settle_seconds=0).pending)

    target.touch()
    scan = scan_changes(ingest_source(root), state, settle_seconds=0)
    assert scan.pending == []
    assert scan.unchanged == 1


def test_settle_skips_recent_files(tmp_path: Path) -> None:
    root = tmp_path / "job"
    _write(root / "Addendum 03.pdf", b"fresh")
    manifest = ingest_source(root)
    state = PackageState(source=str(root), label=manifest.label)
    scan = scan_changes(manifest, state, settle_seconds=3600)
    assert scan.pending == []
    assert scan.settling == 1


def test_legacy_manifest_is_not_reuploaded(tmp_path: Path) -> None:
    root = tmp_path / "job"
    _write(root / "A-101.pdf")
    _write(root / "Addendum 04.pdf", b"new")
    manifest = ingest_source(root)
    legacy = tmp_path / "job.manifest.json"
    legacy.write_text(
        '{"files": [{"relative_path": "A-101.pdf", "category": "drawing"}]}',
        encoding="utf-8",
    )
    state = PackageState(source=str(root), label=manifest.label)
    seed_from_legacy_manifest(state, manifest, legacy)
    scan = scan_changes(manifest, state, settle_seconds=0)
    assert [delta.item.path.name for delta in scan.pending] == ["Addendum 04.pdf"]
