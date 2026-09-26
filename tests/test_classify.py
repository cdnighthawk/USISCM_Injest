from pathlib import Path

from usiscm_ingest.classify import FileCategory, classify_file


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"sample")
    return path


def test_sheet_number_is_a_drawing_without_office_folder_names(tmp_path: Path) -> None:
    path = _touch(tmp_path / "random dump" / "A-101 Level 1 Floor Plan.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.DRAWING
    assert result.sheet_number == "A-101"


def test_dwg_is_a_drawing_even_with_opaque_filename(tmp_path: Path) -> None:
    path = _touch(tmp_path / "outgoing" / "export123.dwg")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.DRAWING


def test_project_manual_is_a_spec(tmp_path: Path) -> None:
    path = _touch(tmp_path / "Volume 1" / "Project Manual.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.SPEC


def test_csi_section_filename_is_a_spec(tmp_path: Path) -> None:
    path = _touch(tmp_path / "09 29 00 Gypsum Board.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.SPEC


def test_instructions_to_bidders_is_bid_instructions(tmp_path: Path) -> None:
    path = _touch(tmp_path / "Procurement" / "Instructions to Bidders.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.BID_INSTRUCTIONS


def test_addendum_filename(tmp_path: Path) -> None:
    path = _touch(tmp_path / "Addendum 03.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.ADDENDA


def test_geotech_report(tmp_path: Path) -> None:
    path = _touch(tmp_path / "Soils Report - Final.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.REPORT


def test_schedule_file(tmp_path: Path) -> None:
    path = _touch(tmp_path / "Baseline Schedule.mpp")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.SCHEDULE


def test_unknown_pdf_is_other(tmp_path: Path) -> None:
    path = _touch(tmp_path / "Meeting Minutes 2024-03-12.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.OTHER


def test_progress_print_style_sheet_still_classifies(tmp_path: Path) -> None:
    """Turner-style dump: no category folders, just printed sheets."""
    path = _touch(tmp_path / "Kaiser Permanente San Rafael - Progress Print _5" / "A201.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.DRAWING
    assert result.sheet_number == "A201"


def test_drawing_in_bid_folder_stays_a_drawing(tmp_path: Path) -> None:
    path = _touch(tmp_path / "Bid Documents" / "S2.01 Foundation Plan.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.DRAWING


def test_addendum_no_4_is_not_a_drawing(tmp_path: Path) -> None:
    path = _touch(tmp_path / "Drawings" / "Addendum No.4.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.ADDENDA
    assert result.sheet_number is None


def test_specs_filename_is_a_spec_even_in_a_drawings_folder(tmp_path: Path) -> None:
    for relative in (
        "Pali_CHS_JAN_2025_Specs.pdf",
        "Drawings/Pali_CHS_JAN_2025_Specs.pdf",
        "Specs.pdf",
    ):
        path = _touch(tmp_path / relative)
        result = classify_file(path, root=tmp_path)
        assert result is not None, relative
        assert result.category == FileCategory.SPEC, (relative, result.category, result.reasons)
        assert result.sheet_number is None


def test_sheet_titled_specifications_stays_a_drawing(tmp_path: Path) -> None:
    path = _touch(tmp_path / "A-101 Wall Specifications.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.DRAWING
    assert result.sheet_number == "A-101"


def test_combined_bid_set_in_drawings_folder_stays_a_document(tmp_path: Path) -> None:
    path = _touch(tmp_path / "Drawings" / "Combined Bid Set.pdf")
    result = classify_file(path, root=tmp_path)
    assert result is not None
    assert result.category == FileCategory.BID_INSTRUCTIONS
    assert result.sheet_number is None


def test_w9_rfp_and_combined_bid_set_are_not_drawings(tmp_path: Path) -> None:
    cases = {
        "W9.pdf": FileCategory.BID_INSTRUCTIONS,
        "W-9.pdf": FileCategory.BID_INSTRUCTIONS,
        "RFP.pdf": FileCategory.BID_INSTRUCTIONS,
        "Combined Bid Set.pdf": FileCategory.BID_INSTRUCTIONS,
        "PKG1.pdf": FileCategory.OTHER,
        "Project Specifications.pdf": FileCategory.SPEC,
    }
    for name, category in cases.items():
        path = _touch(tmp_path / name)
        result = classify_file(path, root=tmp_path)
        assert result is not None, name
        assert result.category == category, (name, result.category, result.reasons)
        assert result.category != FileCategory.DRAWING
        assert result.sheet_number is None


def test_macos_junk_is_skipped(tmp_path: Path) -> None:
    path = _touch(tmp_path / "__MACOSX" / "._A-101.pdf")
    assert classify_file(path, root=tmp_path) is None
