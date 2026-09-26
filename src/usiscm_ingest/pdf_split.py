"""Split multi-page drawing PDFs into one PDF per sheet before upload.

The data-server ingest app owns the split. The website still receives one
native B2 object per sheet (``split_pages`` stays false). Specs, addenda,
bid forms, W-9s, manuals, and combined bid sets are not exploded onto the
Drawings API.

When an estimate folder already exists, sheets are also copied to
``<folder>\\02_Processed\\drawings``. This module never creates an
``Estimates`` root.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from usiscm_ingest.classify import ClassifiedFile, FileCategory, is_non_drawing_filename
from usiscm_ingest.drawing_namer import (
    DrawingName,
    is_sheet_number,
    name_drawing,
    sheet_number_from_page_text,
)

logger = logging.getLogger(__name__)

PROCESSED_DRAWINGS = Path("02_Processed") / "drawings"
_ESTIMATE_ROOT_NAMES = {"estimates"}
_FOLDER_KEYS = (
    "folder_path",
    "estimate_folder",
    "estimateFolder",
    "estimate_path",
    "cm_folder",
)

_PAGE_ADDENDUM_RE = re.compile(
    r"\baddendum\s+(?:no\.?|number|#)\b|\bthis addendum\b",
    re.IGNORECASE,
)
_PAGE_SPEC_RE = re.compile(
    r"\bproject manual\b|\btable of contents\b|\bsection\s+\d{2}\s+\d{2}\s+\d{2}\b",
    re.IGNORECASE,
)
_PAGE_BID_RE = re.compile(
    r"\bform\s+w[-\s]?9\b|\bw[-\s]?9\b|\brequest for proposal\b|"
    r"\binstructions to bidders\b|\bbid form\b",
    re.IGNORECASE,
)


class PdfSplitError(Exception):
    """A multi-page drawing PDF could not be split. Do not upload it whole."""

    def __init__(self, filename: str, message: str) -> None:
        super().__init__(message)
        self.filename = filename


@dataclass
class PdfInspection:
    readable: bool
    page_count: int
    pages: list[tuple[Path, str]] = field(default_factory=list)


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "-", name).strip(" .")
    return (cleaned[:180] or "sheet.pdf")


def _work_stem(path: Path) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", path.stem).strip("._")[:60]
    return cleaned or "drawing"


def category_for_page_text(text: str | None) -> FileCategory | None:
    """Non-drawing category for one page, or None when it is still a sheet.

    A real sheet number wins. General notes that mention specifications do
    not, by themselves, pull a titled sheet onto the documents path.
    """
    sample = (text or "")[:6000]
    if not sample.strip():
        return None
    if sheet_number_from_page_text(sample):
        return None
    if _PAGE_ADDENDUM_RE.search(sample):
        return FileCategory.ADDENDA
    if _PAGE_SPEC_RE.search(sample):
        return FileCategory.SPEC
    if _PAGE_BID_RE.search(sample):
        return FileCategory.BID_INSTRUCTIONS
    return None


def inspect_pdf(path: Path, dest_dir: Path) -> PdfInspection:
    """Open a PDF. Multi-page files are written as one PDF per page."""
    if path.suffix.lower() != ".pdf":
        return PdfInspection(False, 0)
    try:
        import pymupdf
    except ImportError:
        logger.warning("PyMuPDF is not installed; cannot sheet-split %s", path.name)
        return PdfInspection(False, 0)
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        logger.debug("not a readable PDF %s: %s", path, exc)
        return PdfInspection(False, 0)
    try:
        count = int(doc.page_count)
        if count <= 0:
            return PdfInspection(True, 0)
        if count == 1:
            text = doc.load_page(0).get_text("text") or ""
            return PdfInspection(True, 1, [(path, text)])
        dest_dir.mkdir(parents=True, exist_ok=True)
        pages: list[tuple[Path, str]] = []
        stem = _work_stem(path)
        for index in range(count):
            text = doc.load_page(index).get_text("text") or ""
            single = pymupdf.open()
            try:
                single.insert_pdf(doc, from_page=index, to_page=index)
                out = dest_dir / f"{stem}__sheet-{index + 1:04d}.pdf"
                single.save(out)
            except Exception as exc:
                raise PdfSplitError(path.name, f"could not split page {index + 1} of {path.name}: {exc}") from exc
            finally:
                single.close()
            pages.append((out, text))
        if len(pages) != count:
            raise PdfSplitError(path.name, f"split {path.name} produced {len(pages)} of {count} pages")
        return PdfInspection(True, count, pages)
    finally:
        doc.close()


def sheet_output_name(named: DrawingName, page_index: int, parent: Path) -> str:
    """File name for one split sheet. Fallback names are not sheet numbers."""
    number = named.sheet_number if named.sheet_number and is_sheet_number(named.sheet_number) else None
    if not number:
        stem = _work_stem(parent)
        return _safe_filename(f"{stem}__page-{page_index + 1:04d}.pdf")
    stem = number.replace("/", "-")
    title = named.sheet_title or ""
    if title and title.upper().replace(" ", "") != number.upper().replace(" ", ""):
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:40]
        if slug and not slug.lower().startswith("page"):
            stem = f"{stem}_{slug}"
    return _safe_filename(f"{stem}.pdf")


def _unique_name(candidate: str, used: set[str]) -> str:
    if candidate not in used:
        used.add(candidate)
        return candidate
    path = Path(candidate)
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        name = f"{stem}__{n}{suffix}"
        if name not in used:
            used.add(name)
            return name
        n += 1


def _relative(parent_relative: str, filename: str) -> str:
    parent = Path(parent_relative).parent
    if str(parent) in (".", ""):
        return filename
    return str(parent / filename).replace("\\", "/")


def _copy_item(item: ClassifiedFile, **changes: object) -> ClassifiedFile:
    data: dict[str, object] = {
        "path": item.path,
        "relative_path": item.relative_path,
        "category": item.category,
        "confidence": item.confidence,
        "reasons": list(item.reasons),
        "sheet_number": item.sheet_number,
        "size_bytes": item.size_bytes,
        "page_text": item.page_text,
        "from_split": item.from_split,
        "origin_path": item.origin_path or str(item.path),
    }
    data.update(changes)
    return ClassifiedFile(**data)  # type: ignore[arg-type]


def _name_page(
    *,
    parent: ClassifiedFile,
    filename: str,
    page_text: str,
    from_split: bool,
) -> DrawingName:
    return name_drawing(
        filename=filename if from_split else parent.path.name,
        folder_path=parent.relative_path,
        page_text=page_text,
        use_filename_sheet=not from_split,
        use_filename_title=not from_split,
    )


def expand_drawing_file(item: ClassifiedFile, dest_dir: Path) -> list[ClassifiedFile]:
    """One uploadable file per sheet.

    Single-page files and non-PDFs pass through. A multi-page drawing PDF
    becomes one PDF per page. Pages that are addenda, specs, or bid forms
    are retagged so they take the documents path. Combined bid sets and
    other non-drawing filenames are retagged without being split onto Drawings.
    """
    origin = item.origin_path or str(item.path)
    if is_non_drawing_filename(item.path.name) and item.category == FileCategory.DRAWING:
        renamed = _copy_item(
            item,
            category=_document_category(item.path.name),
            sheet_number=None,
            origin_path=origin,
            reasons=[*item.reasons, "non-drawing file kept off the drawings path"],
        )
        return [renamed]

    if item.path.suffix.lower() != ".pdf":
        if not item.origin_path:
            return [_copy_item(item, origin_path=origin)]
        return [item]

    info = inspect_pdf(item.path, dest_dir / _work_stem(item.path))
    if not info.readable or info.page_count <= 1:
        text = info.pages[0][1] if info.pages else item.page_text
        named = _name_page(parent=item, filename=item.path.name, page_text=text or "", from_split=False)
        page_category = category_for_page_text(text)
        category = page_category or item.category
        sheet = named.sheet_number if category == FileCategory.DRAWING else None
        return [
            _copy_item(
                item,
                category=category,
                sheet_number=sheet,
                page_text=text,
                from_split=False,
                origin_path=origin,
            )
        ]

    used: set[str] = set()
    outputs: list[ClassifiedFile] = []
    for index, (pdf, text) in enumerate(info.pages):
        named = _name_page(parent=item, filename=item.path.name, page_text=text, from_split=True)
        page_category = category_for_page_text(text)
        category = page_category or FileCategory.DRAWING
        filename = _unique_name(sheet_output_name(named, index, item.path), used)
        final = pdf.with_name(filename)
        if final != pdf:
            pdf.rename(final)
        try:
            size = final.stat().st_size
        except OSError:
            size = 0
        outputs.append(
            ClassifiedFile(
                path=final,
                relative_path=_relative(item.relative_path, final.name),
                category=category,
                confidence=item.confidence,
                reasons=[*item.reasons, f"split page {index + 1} of {info.page_count} from {item.path.name}"],
                sheet_number=named.sheet_number if category == FileCategory.DRAWING else None,
                size_bytes=size,
                page_text=text,
                from_split=True,
                origin_path=origin,
            )
        )
    logger.info("Split %s into %d sheet PDF(s)", item.path.name, len(outputs))
    return outputs


def _document_category(filename: str) -> FileCategory:
    stem = Path(filename).stem
    if re.search(r"addend|bulletin", stem, re.I):
        return FileCategory.ADDENDA
    if re.search(r"spec|project\s+manual|manual", stem, re.I):
        return FileCategory.SPEC
    if re.search(r"rfp|rfq|bid|w-?\s*9|proposal", stem, re.I):
        return FileCategory.BID_INSTRUCTIONS
    return FileCategory.OTHER


def _leaf_name(path: Path) -> str:
    text = str(path).replace("\\", "/").rstrip("/")
    return text.split("/")[-1] if text else ""


def resolve_estimate_folder(project: dict | None, explicit: str | None = None) -> Path | None:
    """Existing job folder only. Never creates ``Y:\\Estimates`` or any other root."""
    candidates: list[str] = []
    if explicit and str(explicit).strip():
        candidates.append(str(explicit).strip())
    if project:
        for key in _FOLDER_KEYS:
            value = project.get(key)
            if value and str(value).strip():
                candidates.append(str(value).strip())
    for raw in candidates:
        path = Path(raw)
        if _leaf_name(path).lower() in _ESTIMATE_ROOT_NAMES:
            logger.info("estimate folder %s is an Estimates root; not creating a job folder", path)
            continue
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        if exists:
            return path
        logger.info("estimate folder %s is not an existing directory; skipping local sheet copy", path)
    return None


def dual_write_sheet(estimate_folder: Path, source: Path, filename: str) -> Path | None:
    """Copy one sheet into ``<estimate>\\02_Processed\\drawings`` when that folder exists."""
    folder = Path(estimate_folder)
    if _leaf_name(folder).lower() in _ESTIMATE_ROOT_NAMES:
        logger.info("refusing to write sheets under Estimates root %s", folder)
        return None
    try:
        exists = folder.is_dir()
    except OSError:
        exists = False
    if not exists:
        logger.info("estimate folder does not exist (%s); not creating it", folder)
        return None
    dest_dir = folder / PROCESSED_DRAWINGS
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _safe_filename(filename)
    shutil.copyfile(source, dest)
    return dest
