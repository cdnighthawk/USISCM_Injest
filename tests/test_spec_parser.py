"""Spec_Parser gate, missing-install soft fail, and CLI invoke."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from usiscm_ingest.classify import ClassifiedFile, FileCategory
from usiscm_ingest.client import UsiscmClient
from usiscm_ingest.config import Settings
from usiscm_ingest.package import ingest_source
from usiscm_ingest.spec_parser import (
    DEFAULT_SPEC_PARSER_DIR,
    _find_spec_parser_cli,
    _run_spec_parser,
    _should_spec_parse,
    parse_spec_documents,
    spec_parser_dir,
    spec_split_dir,
)


def _spec(path: Path) -> ClassifiedFile:
    return ClassifiedFile(
        path=path,
        relative_path=path.name,
        category=FileCategory.SPEC,
        confidence=0.9,
    )


def test_default_spec_parser_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEC_PARSER_DIR", raising=False)
    monkeypatch.delenv("USISCM_SPEC_PARSER_DIR", raising=False)
    assert spec_parser_dir() == DEFAULT_SPEC_PARSER_DIR
    assert spec_parser_dir() == Path(r"D:\Programs\Spec_Parser")


def test_spec_parser_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    other = tmp_path / "other"
    monkeypatch.setenv("USISCM_SPEC_PARSER_DIR", str(other))
    monkeypatch.setenv("SPEC_PARSER_DIR", str(tmp_path / "primary"))
    assert spec_parser_dir() == tmp_path / "primary"
    monkeypatch.delenv("SPEC_PARSER_DIR")
    assert spec_parser_dir() == other


def test_section_dir_is_under_spec_splits_not_drawings(tmp_path: Path) -> None:
    estimate = tmp_path / "26092"
    pdf = Path("Pali CHS_JAN 2025_Specs.pdf")
    dest = spec_split_dir(estimate, pdf)
    assert dest.parent == estimate / "02_Processed" / "spec_splits"
    assert "drawings" not in {part.lower() for part in dest.relative_to(estimate).parts}
    assert dest.name == "Pali CHS_JAN 2025_Specs"


@pytest.mark.parametrize(
    ("name", "kind", "expected"),
    [
        ("Project Manual.pdf", "specs", True),
        ("Project Manual Vol 2.pdf", "unknown", True),
        ("Pali_CHS_JAN_2025_Specs.pdf", "unknown", True),
        ("Specifications.pdf", "specs", True),
        ("Division 08 Openings.pdf", "unknown", True),
        ("Notice Inviting Bid.pdf", "specs", False),
        ("NIB.pdf", "unknown", False),
        ("Invitation to Bid.pdf", "specs", False),
        ("Instructions to Bidders.pdf", "unknown", False),
        ("Door Hardware.pdf", "specs", False),
        ("Hardware Sets.pdf", "unknown", False),
        ("Addendum Letter.pdf", "specs", False),
        ("08 11 16 - Aluminum Doors.pdf", "specs", False),
        ("081116.pdf", "specs", False),
        ("10_21_13 - Toilet Compartments.pdf", "unknown", False),
        ("Project Manual.pdf", "drawings", False),
        ("random-notes.pdf", "mixed", False),
    ],
)
def test_should_spec_parse_gate(monkeypatch: pytest.MonkeyPatch, name: str, kind: str, expected: bool) -> None:
    monkeypatch.setattr("usiscm_ingest.spec_parser._probe_pdf_kind", lambda pdf: (kind, "test"))
    ok, reason = _should_spec_parse(Path(name))
    assert ok is expected
    if not expected:
        assert reason


def test_gui_py_is_not_an_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parser = tmp_path / "Spec_Parser"
    parser.mkdir()
    (parser / "gui.py").write_text("raise SystemExit('no gui')\n", encoding="utf-8")
    monkeypatch.setenv("SPEC_PARSER_DIR", str(parser))
    assert _find_spec_parser_cli() is None
    (parser / "cli.py").write_text("# cli\n", encoding="utf-8")
    assert _find_spec_parser_cli() == parser / "cli.py"


def test_missing_parser_soft_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "no-such-spec-parser"
    monkeypatch.setenv("SPEC_PARSER_DIR", str(missing))
    pdf = tmp_path / "Project Manual.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really")
    out = tmp_path / "26092" / "02_Processed" / "spec_splits" / "Project Manual"
    result = _run_spec_parser(pdf, out)
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert str(missing) in result["error"]
    assert not out.exists()

    estimate = tmp_path / "26092"
    estimate.mkdir()
    parsed = parse_spec_documents([_spec(pdf)], estimate)
    assert parsed[0]["ok"] is False
    assert not list((estimate / "02_Processed").rglob("*.pdf"))


def test_no_estimate_folder_keeps_whole_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "Specifications.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr("usiscm_ingest.spec_parser._probe_pdf_kind", lambda _pdf: ("specs", "test"))
    called: list[Path] = []
    monkeypatch.setattr(
        "usiscm_ingest.spec_parser._run_spec_parser",
        lambda *args, **kwargs: called.append(args[0]) or {"ok": True},
    )
    results = parse_spec_documents([_spec(pdf)], None)
    assert called == []
    assert results[0]["skipped"] == "no estimate folder"
    assert results[0]["ok"] is False


def test_non_spec_and_split_pages_are_not_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    estimate = tmp_path / "est"
    estimate.mkdir()
    manual = tmp_path / "Project Manual.pdf"
    manual.write_bytes(b"%PDF")
    bid = tmp_path / "Invitation to Bid.pdf"
    bid.write_bytes(b"%PDF")
    page = tmp_path / "sheet.pdf"
    page.write_bytes(b"%PDF")
    called: list[str] = []
    monkeypatch.setattr("usiscm_ingest.spec_parser._probe_pdf_kind", lambda _pdf: ("specs", "test"))
    monkeypatch.setattr(
        "usiscm_ingest.spec_parser._run_spec_parser",
        lambda pdf, out, **kwargs: called.append(pdf.name) or {"pdf": str(pdf), "ok": True, "out": str(out)},
    )
    items = [
        ClassifiedFile(path=bid, relative_path=bid.name, category=FileCategory.BID_INSTRUCTIONS, confidence=0.8),
        ClassifiedFile(
            path=page,
            relative_path=page.name,
            category=FileCategory.SPEC,
            confidence=0.5,
            from_split=True,
        ),
        _spec(manual),
    ]
    parse_spec_documents(items, estimate)
    assert called == ["Project Manual.pdf"]


def test_mocked_cli_invoke_uses_section_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parser = tmp_path / "Spec_Parser"
    parser.mkdir()
    cli = parser / "cli.py"
    cli.write_text("# cli\n", encoding="utf-8")
    monkeypatch.setenv("SPEC_PARSER_DIR", str(parser))
    monkeypatch.delenv("SPEC_PARSER_PYTHON", raising=False)
    monkeypatch.delenv("USISCM_SPEC_PARSER_PYTHON", raising=False)

    pdf = tmp_path / "Project Specifications.pdf"
    pdf.write_bytes(b"%PDF-1.4 manual")
    out = tmp_path / "26092" / "02_Processed" / "spec_splits" / pdf.stem
    captured: dict[str, object] = {}

    class FakeProc:
        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int:
            self.returncode = 0
            return 0

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            return ("sections written\n", "")

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: int | None = None) -> int | None:
            return self.returncode

    def fake_popen(cmd, cwd=None, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        captured["kwargs"] = kwargs
        section_dir = Path(cmd[4])
        section_dir.mkdir(parents=True, exist_ok=True)
        (section_dir / "09 29 00 - Gypsum Board.pdf").write_bytes(b"%PDF-section")
        return FakeProc()

    monkeypatch.setattr("usiscm_ingest.spec_parser.subprocess.Popen", fake_popen)
    result = _run_spec_parser(pdf, out)

    assert result["ok"] is True
    assert result["out"] == str(out)
    cmd = captured["cmd"]
    assert cmd == [
        sys.executable,
        str(cli),
        str(pdf),
        "-o",
        str(out),
        "--by",
        "section",
    ]
    assert captured["cwd"] == str(parser)
    assert (out / "09 29 00 - Gypsum Board.pdf").is_file()
    assert "gui.py" not in " ".join(str(part) for part in cmd)


def test_large_manual_is_chunked_and_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parser = tmp_path / "Spec_Parser"
    parser.mkdir()
    (parser / "cli.py").write_text("# cli\n", encoding="utf-8")
    monkeypatch.setenv("SPEC_PARSER_DIR", str(parser))
    pdf = tmp_path / "Project Manual.pdf"
    pdf.write_bytes(b"%PDF")
    out = tmp_path / "sections"
    monkeypatch.setattr("usiscm_ingest.spec_parser._spec_pdf_stats", lambda _pdf: (900, 10.0))
    chunk_a = tmp_path / "chunk_a.pdf"
    chunk_b = tmp_path / "chunk_b.pdf"
    chunk_a.write_bytes(b"a")
    chunk_b.write_bytes(b"b")
    monkeypatch.setattr(
        "usiscm_ingest.spec_parser._chunk_pdf_pages",
        lambda *_args, **_kwargs: [chunk_a, chunk_b],
    )

    def once(cli: Path, chunk_pdf: Path, dest: Path, **kwargs) -> dict:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "03 30 00 - Cast-in-Place Concrete.pdf").write_bytes(b"%PDF-section")
        return {"pdf": str(chunk_pdf), "ok": True, "out": str(dest)}

    monkeypatch.setattr("usiscm_ingest.spec_parser._run_spec_parser_once", once)
    result = _run_spec_parser(pdf, out)
    assert result["ok"] is True
    assert result["chunked"] is True
    assert result["chunks"] == 2
    assert result["sections_merged"] == 1
    assert (out / "03 30 00 - Cast-in-Place Concrete.pdf").is_file()
    written = list(out.glob("*.pdf"))
    assert len(written) == 1


def _client(tmp_path: Path) -> tuple[UsiscmClient, list[str], list[bytes]]:
    document_names: list[str] = []
    payloads: list[bytes] = []

    def fake_b2(hint, payload, **kwargs):
        payloads.append(payload)
        return {"fileId": "fid", "fileName": hint.get("file_name"), "contentSha1": "abc"}

    def fake_post(url, **kwargs):
        assert "files" not in kwargs
        response = MagicMock()
        response.ok = True
        response.headers = {}
        response.status_code = 201
        if url.rstrip("/").endswith("/drawings"):
            response.json.return_value = {
                "drawing": {"id": "draw-1", "file_pending": True},
                "upload": {
                    "mode": "b2_native",
                    "url": "https://pod.backblaze.com/b2api/v2/b2_upload_file/x",
                    "authorization": "b2tok",
                    "file_name": "drawings/file.pdf",
                },
            }
        elif url.rstrip("/").endswith("/documents"):
            body = kwargs.get("json") or {}
            document_names.append(body.get("filename") or "")
            response.json.return_value = {
                "document": {"id": "doc-1", "file_pending": True},
                "upload": {
                    "mode": "b2_native",
                    "url": "https://pod.backblaze.com/b2api/v2/b2_upload_file/x",
                    "authorization": "b2tok",
                    "file_name": "documents/Project-Manual.pdf",
                },
            }
        elif url.endswith("/ack-file"):
            response.status_code = 200
            response.json.return_value = {"item": {"file_pending": False}}
        else:
            response.status_code = 200
            response.json.return_value = {}
        return response

    client = UsiscmClient(
        Settings(
            base_url="https://www.usiscm.com",
            ingest_api_key="night-key",
            token_path=tmp_path / "ms.json",
            sheet_ai=False,
        ),
        b2_post=fake_b2,
    )
    client.resolve_project = MagicMock(return_value={"id": "job-1", "name": "Job"})  # type: ignore[method-assign]
    client.session.post = MagicMock(side_effect=fake_post)
    return client, document_names, payloads


def test_import_soft_fails_when_parser_missing_and_keeps_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPEC_PARSER_DIR", str(tmp_path / "missing-parser"))
    root = tmp_path / "Job"
    root.mkdir()
    (root / "Project Manual.pdf").write_bytes(b"spec-bytes")
    (root / "A-101.pdf").write_bytes(b"%PDF drawing")
    estimate = tmp_path / "26092"
    estimate.mkdir()
    client, document_names, payloads = _client(tmp_path)
    result = client.import_package(ingest_source(root), project_id="job-1", estimate_folder=str(estimate))
    assert result.errors == []
    assert result.imported == 2
    assert document_names == ["Project Manual.pdf"]
    assert b"spec-bytes" in payloads
    assert result.spec_splits
    assert result.spec_splits[0]["ok"] is False
    assert "not found" in (result.spec_splits[0].get("error") or "")
    assert not list((estimate / "02_Processed" / "spec_splits").rglob("*.pdf"))
    assert not list((estimate / "02_Processed" / "drawings").glob("*Spec*"))


def test_import_writes_section_pdfs_as_documents_not_drawings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "Job"
    root.mkdir()
    (root / "Project Manual.pdf").write_bytes(b"spec-bytes")
    (root / "A-101.pdf").write_bytes(b"%PDF drawing")
    estimate = tmp_path / "26092"
    estimate.mkdir()

    def fake_run(pdf: Path, out: Path, **kwargs) -> dict:
        assert out == estimate / "02_Processed" / "spec_splits" / "Project Manual"
        assert "drawings" not in {part.lower() for part in out.relative_to(estimate).parts}
        out.mkdir(parents=True, exist_ok=True)
        (out / "08 11 16 - Aluminum Doors.pdf").write_bytes(b"%PDF-section")
        return {"pdf": str(pdf), "ok": True, "out": str(out)}

    monkeypatch.setattr("usiscm_ingest.spec_parser._probe_pdf_kind", lambda _pdf: ("unknown", "test"))
    monkeypatch.setattr("usiscm_ingest.spec_parser._run_spec_parser", fake_run)
    client, document_names, payloads = _client(tmp_path)
    result = client.import_package(ingest_source(root), project_id="job-1", estimate_folder=str(estimate))

    section = estimate / "02_Processed" / "spec_splits" / "Project Manual" / "08 11 16 - Aluminum Doors.pdf"
    assert section.is_file()
    assert result.errors == []
    assert document_names == ["Project Manual.pdf"]
    assert "08 11 16 - Aluminum Doors.pdf" not in document_names
    urls = [call.args[0] for call in client.session.post.call_args_list]
    assert any(url.endswith("/api/documents") for url in urls)
    assert any(url.endswith("/api/drawings") for url in urls)
    assert not any("Aluminum" in url or "spec_splits" in url for url in urls)
    assert b"%PDF-section" not in payloads
    assert b"spec-bytes" in payloads
    assert result.spec_splits[0]["ok"] is True
    assert not (estimate / "02_Processed" / "drawings" / "08 11 16 - Aluminum Doors.pdf").exists()
