from pathlib import Path

import pymupdf

from usiscm_ingest.classify import FileCategory, classify_file
from usiscm_ingest.pdf_split import (
    dual_write_sheet,
    expand_drawing_file,
    resolve_estimate_folder,
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
