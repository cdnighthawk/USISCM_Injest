from usiscm_ingest.drawing_namer import (
    apply_ai_identity,
    classify_label,
    discipline_from_sheet_number,
    is_sheet_number,
    name_drawing,
    parse_filename,
    parse_folder_path,
)


def test_parse_common_sheet_filenames() -> None:
    cases = (
        ("A1-001_BCK-1.pdf", "A1-001", "BCK 1"),
        ("G0.1.01_SHEET-INDEX-VOLUME-1_Rev-120.pdf", "G0.1.01", "SHEET INDEX VOLUME 1"),
        ("A10.02.1_FINISH-PLAN-L2-SECTOR-1_Rev-04_Bulletin-15.pdf", "A10.02.1", "FINISH PLAN L2 SECTOR 1"),
        ("A-100 Floor Plan.pdf", "A-100", "Floor Plan"),
        ("P3-G0.1.01_SHEET-INDEX.pdf", "P3-G0.1.01", "SHEET INDEX"),
    )
    for name, sheet, title_part in cases:
        got = parse_filename(name)
        assert got["sheet_number"] == sheet, (name, got)
        assert title_part.lower() in (got["sheet_title"] or "").lower(), (name, got)


def test_revision_from_filename() -> None:
    got = parse_filename("A7.31_SITE_Rev-00_Permit-Set.pdf")
    assert got["sheet_number"] == "A7.31"
    assert got["revision"] == "00"


def test_folder_path_job_discipline_set() -> None:
    got = parse_folder_path("25270/Architectural/Permit-Set/A1-001_BCK-1.pdf")
    assert got["job"] == "25270"
    assert got["discipline"] == "Architectural"
    assert got["drawing_set"] == "Permit-Set"


def test_name_drawing_fills_discipline_and_does_not_need_review() -> None:
    got = name_drawing(filename="A1-001_ENTRY.pdf")
    assert got.sheet_number == "A1-001"
    assert got.discipline == "Architectural"
    assert got.needs_review is False
    assert got.label_status == "ok"


def test_scan_filename_needs_review_but_still_named() -> None:
    got = name_drawing(filename="scan_001.pdf", folder_path="Downloads/scan_001.pdf")
    assert got.needs_review is True
    assert got.sheet_number is None or got.label_status != "ok"
    assert got.review_message()


def test_hygiene_accepts_real_job_sheet_numbers() -> None:
    for num in ("A1-001", "G0.1.01", "A10.02.1", "G0.1.03-A", "P3-G0.1.01"):
        assert is_sheet_number(num), num
        assert classify_label(num)["label_status"] == "ok", num


def test_discipline_from_sheet_number() -> None:
    assert discipline_from_sheet_number("S-101") == "Structural"
    assert discipline_from_sheet_number("E2.01") == "Electrical"
    assert discipline_from_sheet_number("P3-G0.1.01") == "General"


def test_apply_ai_identity_overrides_when_confident() -> None:
    named = name_drawing(filename="scan_001.pdf")
    updated = apply_ai_identity(
        named,
        {
            "sheetNumber": "A-201",
            "sheetTitle": "SECOND FLOOR PLAN",
            "revisionLabel": "Rev 2",
            "confidence": 0.94,
            "needsReview": False,
        },
    )
    assert updated.sheet_number == "A-201"
    assert updated.sheet_title == "SECOND FLOOR PLAN"
    assert updated.needs_review is False
    assert updated.discipline == "Architectural"


def test_apply_ai_identity_flags_garbage_sheet_number() -> None:
    named = name_drawing(filename="A-101 Floor Plan.pdf")
    updated = apply_ai_identity(
        named,
        {"sheetNumber": "Page 12", "sheetTitle": "PLAN", "confidence": 0.4, "needsReview": False},
    )
    assert updated.needs_review is True
    assert updated.sheet_number == "A-101"
