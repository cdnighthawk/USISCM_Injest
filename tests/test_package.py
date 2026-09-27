import io
import json
import zipfile
from pathlib import Path

from usiscm_ingest.classify import FileCategory
from usiscm_ingest.cli import main
from usiscm_ingest.client import UploadResult, _best_project_match
from usiscm_ingest.package import ingest_source, iter_packages, package_label, package_state_key


def _write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _zip_tree(root: Path, dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w") as archive:
        for file in root.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(root))
    return dest


def test_turner_style_progress_print_zip_is_not_required_naming(tmp_path: Path) -> None:
    inner = tmp_path / "build"
    _write(inner / "A-101.pdf")
    _write(inner / "S301.pdf")
    _write(inner / "Project Manual.pdf")
    zip_path = _zip_tree(inner, tmp_path / "Kaiser Permanente San Rafael - Progress Print _5.zip")

    manifest = ingest_source(zip_path, work_dir=tmp_path / "work")
    assert manifest.label == "Kaiser Permanente San Rafael"
    assert manifest.counts[FileCategory.DRAWING.value] == 2
    assert manifest.counts[FileCategory.SPEC.value] == 1


def test_numbered_gc_folder_layout(tmp_path: Path) -> None:
    root = tmp_path / "Sutter Health Oakland MOB"
    _write(root / "01 Drawings" / "Architectural" / "A-201.pdf")
    _write(root / "02 Specifications" / "Project Manual Vol 2.pdf")
    _write(root / "03 Bidding" / "Invitation to Bid.pdf")
    _write(root / "04 Addenda" / "Addendum 01.pdf")
    _write(root / "05 Reports" / "Geotechnical Investigation.pdf")

    manifest = ingest_source(root)
    assert manifest.label == "Sutter Health Oakland MOB"
    assert manifest.counts[FileCategory.DRAWING.value] == 1
    assert manifest.counts[FileCategory.SPEC.value] == 1
    assert manifest.counts[FileCategory.BID_INSTRUCTIONS.value] == 1
    assert manifest.counts[FileCategory.ADDENDA.value] == 1
    assert manifest.counts[FileCategory.REPORT.value] == 1


def test_flat_office_dump_without_folders(tmp_path: Path) -> None:
    root = tmp_path / "Webcor Bid Set"
    _write(root / "ITB.pdf")
    _write(root / "M-401.pdf")
    _write(root / "09 91 00 Painting.pdf")
    _write(root / "Baseline Schedule.mpp")

    manifest = ingest_source(root)
    by_name = {item.path.name: item.category for item in manifest.files}
    assert by_name["ITB.pdf"] == FileCategory.BID_INSTRUCTIONS
    assert by_name["M-401.pdf"] == FileCategory.DRAWING
    assert by_name["09 91 00 Painting.pdf"] == FileCategory.SPEC
    assert by_name["Baseline Schedule.mpp"] == FileCategory.SCHEDULE


def test_zip_slip_is_ignored(tmp_path: Path) -> None:
    zip_path = tmp_path / "evil.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("safe/A-101.pdf", b"ok")
        info = zipfile.ZipInfo("../outside.pdf")
        archive.writestr(info, b"nope")
    zip_path.write_bytes(buffer.getvalue())

    manifest = ingest_source(zip_path, work_dir=tmp_path / "work")
    names = [item.path.name for item in manifest.files]
    assert "A-101.pdf" in names
    assert "outside.pdf" not in names


def test_package_label_does_not_require_progress_print() -> None:
    assert package_label(Path("St Joseph Medical Center Bid Set.zip")) == "St Joseph Medical Center Bid Set"
    assert package_label(Path("Foo - Progress Print _5.zip")) == "Foo"


def test_classify_cli_writes_manifest(tmp_path: Path, capsys) -> None:
    root = tmp_path / "pkg"
    _write(root / "A101.pdf")
    out = tmp_path / "manifest.json"
    assert main(["classify", str(root), "--json", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["counts"]["drawing"] == 1
    printed = capsys.readouterr().out
    assert "drawing" in printed


def test_iter_packages_expands_acc_hub_layout(tmp_path: Path) -> None:
    hub = tmp_path / "USIS Account"
    project = hub / "Kaiser Permanente San Rafael"
    _write(project / "Project Files" / "A-101.pdf")
    other = tmp_path / "Sutter Health Oakland MOB"
    _write(other / "A-201.pdf")
    zip_path = tmp_path / "extra.zip"
    zip_path.write_bytes(b"PK\x03\x04")

    packages = {path.name for path in iter_packages(tmp_path)}
    assert packages == {"Kaiser Permanente San Rafael", "Sutter Health Oakland MOB", "extra.zip"}
    assert "USIS Account" not in packages


def test_acc_state_key_uses_hub_and_project(tmp_path: Path) -> None:
    project = tmp_path / "Hub" / "Job"
    project.mkdir(parents=True)
    assert package_state_key(tmp_path, project) == "Hub__Job"


def test_watch_ingests_new_addenda_without_reuploading_old_files(tmp_path: Path, monkeypatch) -> None:
    accdocs = tmp_path / "ACCDocs"
    project = accdocs / "Account" / "Kaiser MOB"
    _write(project / "Drawings" / "A-101.pdf")
    state = tmp_path / "USISCM-ingest"
    monkeypatch.setenv("USISCM_WATCH_DIR", str(accdocs))
    monkeypatch.setenv("USISCM_PROCESSED_DIR", str(state / "processed"))
    monkeypatch.setenv("USISCM_FAILED_DIR", str(state / "failed"))
    monkeypatch.setenv("USISCM_SETTLE_SECONDS", "0")
    monkeypatch.setenv("USISCM_EMAIL", "user@example.com")
    monkeypatch.setenv("USISCM_PASSWORD", "secret")

    class FakeClient:
        calls: list[list[str]] = []

        def import_package(self, manifest, project_id=None, dry_run=False):
            names = [item.path.name for item in manifest.files]
            FakeClient.calls.append(names)
            return UploadResult(project_id=project_id or 1, imported=len(manifest.files))

    monkeypatch.setattr("usiscm_ingest.cli._client", lambda settings: FakeClient())

    assert main(["watch", "--once"]) == 0
    assert FakeClient.calls == [["A-101.pdf"]]
    sidecar = state / "processed" / "Account__Kaiser MOB.state.json"
    assert sidecar.is_file()

    assert main(["watch", "--once"]) == 0
    assert FakeClient.calls == [["A-101.pdf"]]

    _write(project / "Addenda" / "Addendum 02.pdf", b"addendum-two")
    assert main(["watch", "--once"]) == 0
    assert FakeClient.calls == [["A-101.pdf"], ["Addendum 02.pdf"]]
    assert project.exists()


def test_watch_reprocess_can_target_one_package(tmp_path: Path, monkeypatch) -> None:
    accdocs = tmp_path / "ACCDocs"
    _write(accdocs / "Account" / "26092" / "Drawings" / "A-101.pdf")
    _write(accdocs / "Account" / "99999" / "Drawings" / "A-201.pdf")
    state = tmp_path / "USISCM-ingest"
    monkeypatch.setenv("USISCM_WATCH_DIR", str(accdocs))
    monkeypatch.setenv("USISCM_PROCESSED_DIR", str(state / "processed"))
    monkeypatch.setenv("USISCM_FAILED_DIR", str(state / "failed"))
    monkeypatch.setenv("USISCM_SETTLE_SECONDS", "0")

    class FakeClient:
        calls: list[str] = []

        def import_package(self, manifest, project_id=None, dry_run=False):
            FakeClient.calls.append(manifest.label)
            return UploadResult(project_id=project_id or 1, imported=len(manifest.files))

    monkeypatch.setattr("usiscm_ingest.cli._client", lambda settings: FakeClient())
    assert main(["watch", "--once", "--reprocess", "--package", "26092"]) == 0
    assert FakeClient.calls == ["26092"]


def test_watch_package_filter_miss_is_an_error(tmp_path: Path, monkeypatch, caplog) -> None:
    accdocs = tmp_path / "ACCDocs"
    _write(accdocs / "Account" / "26092" / "A-101.pdf")
    monkeypatch.setenv("USISCM_WATCH_DIR", str(accdocs))
    monkeypatch.setenv("USISCM_PROCESSED_DIR", str(tmp_path / "processed"))
    monkeypatch.setenv("USISCM_FAILED_DIR", str(tmp_path / "failed"))
    with caplog.at_level("ERROR"):
        assert main(["watch", "--once", "--dry-run", "--package", "missing-job"]) == 2
    assert "matched" in caplog.text


def test_watch_missing_accdocs_errors(tmp_path: Path, monkeypatch, caplog) -> None:
    missing = tmp_path / "no-such-accdocs"
    monkeypatch.setenv("USISCM_WATCH_DIR", str(missing))
    with caplog.at_level("ERROR"):
        assert main(["watch", "--once", "--dry-run"]) == 2
    assert "does not exist" in caplog.text


def test_project_match_uses_token_overlap() -> None:
    projects = [
        {"id": 1, "project_name": "Kaiser Permanente San Rafael MOB", "project_number": "KP-SR-01"},
        {"id": 2, "project_name": "Sutter Oakland", "project_number": "SO-9"},
    ]
    match = _best_project_match("Kaiser Permanente San Rafael", projects)
    assert match is not None
    assert match["id"] == 1
