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


def test_junk_filename_tokens_are_not_sheet_numbers() -> None:
    from usiscm_ingest.drawing_namer import is_junk_sheet_token

    for name in ("Addendum No.4.pdf", "ADD01.pdf", "PKG1.pdf", "W9.pdf", "W-9.pdf", "Form W9.pdf"):
        got = parse_filename(name)
        assert got["sheet_number"] is None, (name, got)
        named = name_drawing(filename=name)
        assert named.sheet_number is None, name
    for token in ("NO.4", "ADD01", "PKG1", "W9", "W-9"):
        assert is_junk_sheet_token(token)
        assert not is_sheet_number(token)


def test_real_sheet_numbers_still_parse() -> None:
    assert parse_filename("A-101 Floor Plan.pdf")["sheet_number"] == "A-101"
    assert is_sheet_number("P3-G0.1.01")
    assert is_sheet_number("S2.01")


def test_newline_is_not_part_of_a_sheet_number() -> None:
    from usiscm_ingest.drawing_namer import identity_from_page_text, normalize_sheet_number

    assert is_sheet_number("NEW\n3") is False
    assert normalize_sheet_number("NEW\n3") is None
    text = "SHEET TITLE: 100-West-Villa-Street-Suite-101\nSHEET NO.\nNEW\n3\n"
    identity = identity_from_page_text(text)
    assert identity["sheet_number"] is None or "\n" not in (identity["sheet_number"] or "")
    named = name_drawing(
        filename="ADD_01_HVAC_DWG.pdf",
        page_text=text,
        use_filename_sheet=False,
        use_filename_title=False,
    )
    assert named.sheet_number is None or "\n" not in named.sheet_number
    assert named.sheet_title is None or "\n" not in named.sheet_title


def test_explicit_title_newline_is_stored_as_one_line() -> None:
    named = name_drawing(
        filename="A-101.pdf",
        sheet_number="A-101",
        sheet_title="NEW\n3_100-West-Villa-Street-Suite-101",
        use_filename_sheet=False,
        use_filename_title=False,
    )
    assert named.sheet_number == "A-101"
    assert named.sheet_title == "NEW 3_100-West-Villa-Street-Suite-101"


def test_page_text_names_sheet_after_split_ignores_parent_filename() -> None:
    text = "FLOOR PLAN\nLEVEL 1\nSHEET NO. A-101\nSHEET TITLE: FLOOR PLAN\nSCALE: 1/8\" = 1'-0\""
    named = name_drawing(
        filename="PKG1_Drawings.pdf",
        page_text=text,
        use_filename_sheet=False,
        use_filename_title=False,
    )
    assert named.sheet_number == "A-101"
    assert named.sheet_title == "FLOOR PLAN"
    assert named.discipline == "Architectural"
    assert named.needs_review is False
    assert "PKG1" not in (named.sheet_number or "")


def test_apply_ai_identity_flags_garbage_sheet_number() -> None:
    named = name_drawing(filename="A-101 Floor Plan.pdf")
    updated = apply_ai_identity(
        named,
        {"sheetNumber": "Page 12", "sheetTitle": "PLAN", "confidence": 0.4, "needsReview": False},
    )
    assert updated.needs_review is True
    assert updated.sheet_number == "A-101"
