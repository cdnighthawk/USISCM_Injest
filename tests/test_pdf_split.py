from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest

from usiscm_ingest.classify import FileCategory, classify_file
from usiscm_ingest.client import UploadResult, UsiscmClient
from usiscm_ingest.config import Settings
from usiscm_ingest.package import ingest_source
from usiscm_ingest.drawing_namer import DrawingName
from usiscm_ingest.pdf_split import (
    PdfSplitError,
    _unique_name,
    dual_write_sheet,
    expand_drawing_file,
    resolve_estimate_folder,
    sheet_output_name,
)


def _write_pdf(path: Path, pages: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    try:
        for kind, number, title in pages:
            page = doc.new_page(width=612, height=792)
            if kind == "drawing":
                page.insert_text((72, 72), title)
                page.insert_text((72, 96), f"SHEET NO. {number}")
                page.insert_text((72, 120), f"SHEET TITLE: {title}")
            elif kind == "addendum":
                page.insert_text((72, 72), f"ADDENDUM NO. {number}")
                page.insert_text((72, 96), "This addendum modifies the bid.")
            else:
                page.insert_text((72, 72), title)
        doc.save(path)
    finally:
        doc.close()


def _page_count(path: Path) -> int:
    doc = pymupdf.open(path)
    try:
        return int(doc.page_count)
    finally:
        doc.close()


def test_multipage_drawing_splits_and_names_from_page_text(tmp_path: Path) -> None:
    source = tmp_path / "Drawings" / "Architectural.pdf"
    _write_pdf(
        source,
        [
            ("drawing", "A-101", "FLOOR PLAN"),
            ("drawing", "S-201", "FOUNDATION PLAN"),
            ("drawing", "E-301", "LIGHTING PLAN"),
            ("addendum", "4", ""),
        ],
    )
    item = classify_file(source, root=tmp_path)
    assert item is not None
    assert item.category == FileCategory.DRAWING

    sheets = expand_drawing_file(item, tmp_path / "work")
    drawings = [sheet for sheet in sheets if sheet.category == FileCategory.DRAWING]
    documents = [sheet for sheet in sheets if sheet.category != FileCategory.DRAWING]

    assert [sheet.sheet_number for sheet in drawings] == ["A-101", "S-201", "E-301"]
    assert [sheet.sheet_number for sheet in documents] == [None]
    assert documents[0].category == FileCategory.ADDENDA
    assert all(sheet.from_split for sheet in sheets)
    assert all(_page_count(sheet.path) == 1 for sheet in sheets)
    assert all("PKG" not in sheet.path.name and "NO.4" not in sheet.path.name for sheet in drawings)
    assert "A-101" in drawings[0].path.name
    assert drawings[0].origin_path == str(source.resolve())


def test_single_page_drawing_is_not_rewritten(tmp_path: Path) -> None:
    source = tmp_path / "A-101 Floor Plan.pdf"
    _write_pdf(source, [("drawing", "A-101", "FLOOR PLAN")])
    item = classify_file(source, root=tmp_path)
    assert item is not None
    sheets = expand_drawing_file(item, tmp_path / "work")
    assert len(sheets) == 1
    assert sheets[0].path == source.resolve()
    assert sheets[0].from_split is False
    assert sheets[0].sheet_number == "A-101"
    assert _page_count(sheets[0].path) == 1


def test_dual_write_requires_an_existing_job_folder(tmp_path: Path) -> None:
    source = tmp_path / "Drawings" / "Architectural.pdf"
    _write_pdf(source, [("drawing", "A-101", "FLOOR PLAN"), ("drawing", "S-201", "FOUNDATION PLAN")])
    item = classify_file(source, root=tmp_path)
    assert item is not None
    sheets = [sheet for sheet in expand_drawing_file(item, tmp_path / "work") if sheet.category == FileCategory.DRAWING]

    job = tmp_path / "26092"
    job.mkdir()
    written = [dual_write_sheet(job, sheet.path, sheet.path.name) for sheet in sheets]
    assert all(path is not None and path.is_file() for path in written)
    assert {path.name for path in written if path is not None} == {sheet.path.name for sheet in sheets}
    assert (job / "02_Processed" / "drawings").is_dir()

    missing = tmp_path / "Estimates" / "26092"
    assert dual_write_sheet(missing, sheets[0].path, "A-101.pdf") is None
    assert not missing.exists()
    assert not (tmp_path / "Estimates").exists()

    root = tmp_path / "Estimates"
    root.mkdir()
    assert resolve_estimate_folder({"folder_path": str(root)}) is None
    assert dual_write_sheet(root, sheets[0].path, "A-101.pdf") is None
    assert not (root / "02_Processed").exists()
    assert resolve_estimate_folder({}, r"Y:\Estimates\26092") is None
    assert resolve_estimate_folder({"estimate_folder": str(job)}) == job


def _three_sheet_pdf(tmp_path: Path) -> Path:
    source = tmp_path / "Drawings" / "Palisades Charter HS - HVAC_DWG.pdf"
    _write_pdf(
        source,
        [
            ("drawing", "M-101", "HVAC PLAN"),
            ("drawing", "M-102", "HVAC SCHEDULE"),
            ("drawing", "M-103", "HVAC DETAILS"),
        ],
    )
    return source


def test_split_continues_after_one_page_fails(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    source = _three_sheet_pdf(tmp_path)
    item = classify_file(source, root=tmp_path)
    assert item is not None

    def fail_overflow(doc, index: int, out: Path) -> None:
        if index == 1:
            raise RuntimeError("code=5: exception stack overflow!")
        _real_mupdf_write(doc, index, out)

    def fail_fallback(path: Path, index: int, out: Path) -> str:
        raise RuntimeError("fallback also failed")

    with caplog.at_level("ERROR"):
        with patch("usiscm_ingest.pdf_split._write_page_mupdf", side_effect=fail_overflow):
            with patch("usiscm_ingest.pdf_split._fallback_split_page", side_effect=fail_fallback):
                failures: list[str] = []
                sheets = expand_drawing_file(item, tmp_path / "work", failures=failures)

    drawings = [sheet for sheet in sheets if sheet.category == FileCategory.DRAWING]
    assert [sheet.sheet_number for sheet in drawings] == ["M-101", "M-103"]
    assert all(sheet.from_split for sheet in sheets)
    assert all(_page_count(sheet.path) == 1 for sheet in sheets)
    assert all(sheet.path != source.resolve() for sheet in sheets)
    assert any("split page 1 of 3" in " ".join(sheet.reasons) for sheet in drawings)
    assert any("split page 3 of 3" in " ".join(sheet.reasons) for sheet in drawings)
    assert failures == [f"could not split page(s) 2 of {source.name}"]
    assert f"could not split page 2 of {source.name}" in caplog.text
    assert "stack overflow" in caplog.text
    assert f"skipped page(s) 2" in caplog.text


def test_overflow_page_uses_pypdf_fallback(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    source = _three_sheet_pdf(tmp_path)
    item = classify_file(source, root=tmp_path)
    assert item is not None

    def fail_only_schedule(doc, index: int, out: Path) -> None:
        if index == 1:
            raise RuntimeError("code=5: exception stack overflow!")
        _real_mupdf_write(doc, index, out)

    with caplog.at_level("WARNING"):
        with patch("usiscm_ingest.pdf_split._write_page_mupdf", side_effect=fail_only_schedule):
            sheets = expand_drawing_file(item, tmp_path / "work")

    drawings = [sheet for sheet in sheets if sheet.category == FileCategory.DRAWING]
    assert [sheet.sheet_number for sheet in drawings] == ["M-101", "M-102", "M-103"]
    assert all(_page_count(sheet.path) == 1 for sheet in sheets)
    assert "split page 2 of" in caplog.text
    assert "fallback" in caplog.text


def _real_mupdf_write(doc, index: int, out: Path) -> None:
    import pymupdf

    single = pymupdf.open()
    try:
        single.insert_pdf(doc, from_page=index, to_page=index)
        single.save(out)
    finally:
        single.close()


def test_client_keeps_other_sheets_when_one_page_fails(tmp_path: Path) -> None:
    source = _three_sheet_pdf(tmp_path)
    manifest = ingest_source(tmp_path)
    client = UsiscmClient(
        Settings(base_url="https://www.usiscm.com", token_path=tmp_path / "ms.json", sheet_ai=False)
    )
    client.token = "ms-token"
    client.auth_mode = "microsoft"
    result = UploadResult(project_id="job-1", batch_id="batch")

    def fail_overflow(doc, index: int, out: Path) -> None:
        if index == 1:
            raise RuntimeError("code=5: exception stack overflow!")
        _real_mupdf_write(doc, index, out)

    with patch("usiscm_ingest.pdf_split._write_page_mupdf", side_effect=fail_overflow):
        with patch("usiscm_ingest.pdf_split._fallback_split_page", side_effect=RuntimeError("fallback also failed")):
            drawings, _documents = client._expand_drawings(
                manifest, tmp_path / "work", result, report_failures=False
            )

    assert [sheet.sheet_number for sheet in drawings] == ["M-101", "M-103"]
    assert any(
        detail.get("filename") == source.name and "page(s) 2" in detail.get("error", "")
        for detail in result.details
    )
    assert all(sheet.path.name != source.name for sheet in drawings)


def _shared_unused_resource_pdf(tmp_path: Path) -> Path:
    """Multi-page drawing whose every page names the same unused images.

    A page copy that does not rewrite the file keeps those images, so each
    one-page PDF is about as large as the whole set.
    """
    import os

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    source = tmp_path / "Drawings" / "ADD_01_HVAC_DWG.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    image_refs = []
    for index in range(4):
        image = DecodedStreamObject()
        image.set_data(os.urandom(900_000))
        image.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(900),
                NameObject("/Height"): NumberObject(1000),
                NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            }
        )
        image_refs.append(writer._add_object(image))

    sheets = ("M-101", "M-102", "M-103", "M-104")
    for sheet in sheets:
        page = writer.add_blank_page(width=612, height=792)
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 72 700 Td (SHEET NO. {sheet}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
        xobjects = DictionaryObject(
            {NameObject(f"/Unused{index}"): ref for index, ref in enumerate(image_refs)}
        )
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): xobjects})
    with source.open("wb") as handle:
        writer.write(handle)
    return source


def test_split_pages_are_not_copies_of_the_whole_file(tmp_path: Path) -> None:
    source = _shared_unused_resource_pdf(tmp_path)
    source_size = source.stat().st_size
    doc = pymupdf.open(source)
    naive = tmp_path / "naive.pdf"
    try:
        _real_mupdf_write(doc, 0, naive)
    finally:
        doc.close()
    assert naive.stat().st_size > int(source_size * 0.85)
    assert _page_count(naive) == 1

    item = classify_file(source, root=tmp_path)
    assert item is not None
    assert item.category == FileCategory.DRAWING
    sheets = expand_drawing_file(item, tmp_path / "work")
    assert [sheet.sheet_number for sheet in sheets] == ["M-101", "M-102", "M-103", "M-104"]
    assert all(sheet.from_split for sheet in sheets)
    assert all(sheet.path != source.resolve() for sheet in sheets)
    assert all(_page_count(sheet.path) == 1 for sheet in sheets)
    sizes = [sheet.path.stat().st_size for sheet in sheets]
    assert all(size * 2 < source_size for size in sizes)
    assert sum(sizes) < source_size


def test_page_that_stays_document_sized_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    source = _shared_unused_resource_pdf(tmp_path)

    def save_without_cleanup(doc, out: Path) -> None:
        doc.save(out)

    with caplog.at_level("ERROR"):
        with patch("usiscm_ingest.pdf_split._save_compact", side_effect=save_without_cleanup):
            item = classify_file(source, root=tmp_path)
            assert item is not None
            with pytest.raises(PdfSplitError, match="failed pages: 1, 2, 3, 4"):
                expand_drawing_file(item, tmp_path / "work")
    assert "near the full" in caplog.text
    leftovers = list((tmp_path / "work").rglob("*.pdf"))
    assert leftovers == []


def _dirty_title_name(*, sheet_number: str | None, title: str) -> DrawingName:
    return DrawingName(
        sheet_number=sheet_number,
        sheet_title=title,
        discipline=None,
        drawing_set=None,
        revision="0",
        confidence=0.9,
        needs_review=False,
        label_status="ok",
    )


def test_newline_sheet_title_is_one_windows_filename(tmp_path: Path) -> None:
    title = "NEW\n3_100-West-Villa-Street-Suite-101"
    named = _dirty_title_name(sheet_number="A-101", title=title)
    filename = sheet_output_name(named, 10, Path("ADD_01_HVAC_DWG.pdf"))
    assert "\n" not in filename and "\r" not in filename and "\t" not in filename
    assert not any(char in filename for char in '<>:"/\\|?*')
    assert filename.endswith(".pdf")
    assert not filename[:-4].endswith(" ") and not filename[:-4].endswith(".")
    assert "NEW" in filename and "100-West-Villa" in filename

    work = tmp_path / "sheets"
    work.mkdir()
    source = work / "ADD_01__sheet-0011.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    final = source.with_name(filename)
    assert final.parent == work
    source.rename(final)
    assert final.is_file()
    assert final.name == filename
    assert not (work / "NEW").exists()


def test_sanitized_title_collision_appends_page_index() -> None:
    parent = Path("ADD_01_HVAC_DWG.pdf")
    first = sheet_output_name(
        _dirty_title_name(sheet_number="A-101", title="NEW\n3_100-West-Villa-Street-Suite-101"),
        0,
        parent,
    )
    second = sheet_output_name(
        _dirty_title_name(sheet_number="A-101", title="NEW\r3_100-West-Villa-Street-Suite-101"),
        1,
        parent,
    )
    assert first == second
    used: set[str] = set()
    kept = _unique_name(first, used, page_index=0)
    other = _unique_name(second, used, page_index=1)
    assert kept == first
    assert other != kept
    assert "__p0002" in other
    assert "\n" not in other and "\\" not in other


def test_ocr_newline_sheet_number_does_not_split_the_path(tmp_path: Path) -> None:
    source = tmp_path / "Drawings" / "ADD_01_HVAC_DWG.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    try:
        for lines in (
            [
                "SHEET TITLE: 100-West-Villa-Street-Suite-101",
                "SHEET NO.",
                "NEW",
                "3",
            ],
            ["SHEET NO. M-102", "SHEET TITLE: HVAC SCHEDULE"],
        ):
            page = doc.new_page(width=612, height=792)
            y = 72
            for line in lines:
                page.insert_text((72, y), line)
                y += 18
        doc.save(source)
    finally:
        doc.close()

    item = classify_file(source, root=tmp_path)
    assert item is not None
    sheets = expand_drawing_file(item, tmp_path / "work")
    assert len(sheets) == 2
    assert all(sheet.path != source.resolve() for sheet in sheets)
    stem_dir = tmp_path / "work" / "ADD_01_HVAC_DWG"
    for sheet in sheets:
        assert sheet.path.parent == stem_dir
        assert sheet.path.is_file()
        assert "\n" not in sheet.path.name and "\r" not in sheet.path.name
        assert not any(char in sheet.path.name for char in '<>:"/\\|?*')
    assert "NEW" not in {part for sheet in sheets for part in sheet.path.parts}
    assert not (stem_dir / "NEW").exists()
    assert any(sheet.sheet_number == "M-102" for sheet in sheets)


def test_dual_write_sanitizes_newline_in_filename(tmp_path: Path) -> None:
    source = tmp_path / "A-101.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    job = tmp_path / "26092"
    job.mkdir()
    written = dual_write_sheet(job, source, "NEW\n3_100-West-Villa-Street-Suite-101.pdf")
    assert written is not None
    assert written.parent == job / "02_Processed" / "drawings"
    assert written.is_file()
    assert "\n" not in written.name
    assert "100-West-Villa" in written.name
    assert not (job / "02_Processed" / "drawings" / "NEW").exists()


def test_every_page_failing_does_not_return_the_whole_file(tmp_path: Path) -> None:
    source = _three_sheet_pdf(tmp_path)
    item = classify_file(source, root=tmp_path)
    assert item is not None
    with patch("usiscm_ingest.pdf_split._write_page_mupdf", side_effect=RuntimeError("code=5: exception stack overflow!")):
        with patch("usiscm_ingest.pdf_split._fallback_split_page", side_effect=RuntimeError("nope")):
            with pytest.raises(PdfSplitError, match="failed pages: 1, 2, 3"):
                expand_drawing_file(item, tmp_path / "work")
