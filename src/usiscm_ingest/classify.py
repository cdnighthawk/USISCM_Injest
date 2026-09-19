"""Format-agnostic classification of estimate-package files.

GC offices (Turner, Swinerton, Webcor, etc.) each ship bid sets differently.
This module never requires a specific folder name, zip suffix, or revision
scheme. It scores path, filename, extension, optional sheet-number patterns,
and optional PDF first-page text, then picks the strongest category.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

# Architectural / MEP sheet numbers appear across almost every office's set,
# regardless of how they name the parent folder or zip.
# Examples: A-101, A101, S2.01, M-001, E201, FA-101, P001, I-0.01
SHEET_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:[A-Z]{1,3}[-.]?\d{1,4}(?:[.-]\d{1,3})?)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)

CSI_SECTION_RE = re.compile(r"\b\d{2}\s+\d{2}\s+\d{2}\b")

SKIP_NAME_PARTS = (
    "__macosx",
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
    ".git",
)

DRAWING_EXTENSIONS = {".pdf", ".dwg", ".dxf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt", ".csv"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


class FileCategory(str, Enum):
    DRAWING = "drawing"
    SPEC = "spec"
    BID_INSTRUCTIONS = "bid_instructions"
    ADDENDA = "addenda"
    REPORT = "report"
    SCHEDULE = "schedule"
    OTHER = "other"

    @property
    def document_type(self) -> str:
        """USISCM ``document_type`` for the official ingest / documents APIs."""
        return {
            FileCategory.DRAWING: "drawing",
            FileCategory.SPEC: "specification",
            FileCategory.BID_INSTRUCTIONS: "other",
            FileCategory.ADDENDA: "other",
            FileCategory.REPORT: "report",
            FileCategory.SCHEDULE: "other",
            FileCategory.OTHER: "other",
        }[self]


@dataclass(frozen=True)
class CategoryHints:
    """Substring lists used for scoring. All matches are case-insensitive."""

    folders: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()
    text: tuple[str, ...] = ()


DEFAULT_HINTS: dict[FileCategory, CategoryHints] = {
    FileCategory.DRAWING: CategoryHints(
        folders=(
            "drawing",
            "drawings",
            "plans",
            "plan set",
            "sheets",
            "sheet",
            "architectural",
            "structural",
            "mechanical",
            "electrical",
            "plumbing",
            "civil",
            "mep",
            "fire protection",
            "landscape",
            "dwg",
            "dwgs",
        ),
        filenames=(
            "drawing",
            "drawings",
            "plan set",
            "plans",
            "sheet",
            "title sheet",
            "floor plan",
            "reflected ceiling",
            "rcp",
        ),
        text=(
            "sheet number",
            "drawing no",
            "drawing number",
            "as indicated",
            "scale:",
            "title block",
        ),
    ),
    FileCategory.SPEC: CategoryHints(
        folders=(
            "spec",
            "specs",
            "specification",
            "specifications",
            "project manual",
            "project manuals",
            "spec book",
            "specbook",
            "technical specs",
        ),
        filenames=(
            "specification",
            "specifications",
            "spec book",
            "project manual",
            "projectmanual",
            "tech spec",
            "technical specification",
            "div 01",
            "division 01",
            "general requirements",
        ),
        text=(
            "project manual",
            "specifications",
            "table of contents",
            "section 01",
            "division 01",
            "general requirements",
        ),
    ),
    FileCategory.BID_INSTRUCTIONS: CategoryHints(
        folders=(
            "bid",
            "bidding",
            "itb",
            "invitation",
            "procurement",
            "bid docs",
            "bid documents",
            "bid package",
            "instructions",
        ),
        filenames=(
            "instruction to bidder",
            "instructions to bidders",
            "invitation to bid",
            "invitation for bid",
            "itb",
            "bid form",
            "bid instructions",
            "proposal form",
            "bid proposal",
            "scope of work",
            "trade package",
            "work assignment",
            "prebid",
            "pre-bid",
            "bid package",
            "division 00",
            "div 00",
        ),
        text=(
            "instructions to bidders",
            "invitation to bid",
            "bid form",
            "bid date",
            "proposal form",
            "submit bids",
            "bid bond",
        ),
    ),
    FileCategory.ADDENDA: CategoryHints(
        folders=("addendum", "addenda", "bulletin", "bulletins", "asi"),
        filenames=(
            "addendum",
            "addenda",
            "bulletin",
            "asi ",
            "architects supplemental",
        ),
        text=("addendum no", "addendum number", "this addendum", "bulletin no"),
    ),
    FileCategory.REPORT: CategoryHints(
        folders=(
            "report",
            "reports",
            "geotech",
            "geotechnical",
            "soils",
            "survey",
            "environmental",
            "hazmat",
        ),
        filenames=(
            "geotech",
            "geotechnical",
            "soils report",
            "survey",
            "phase i",
            "phase 1 esa",
            "environmental",
            "asbestos",
            "lead report",
            "investigation",
        ),
        text=("geotechnical", "subsurface", "boring log", "environmental site"),
    ),
    FileCategory.SCHEDULE: CategoryHints(
        folders=("schedule", "schedules", "cpm", "lookahead"),
        filenames=(
            "schedule",
            "cpm",
            "gantt",
            "baseline schedule",
            "construction schedule",
            "lookahead",
        ),
        text=("critical path", "activity id", "baseline schedule"),
    ),
}


@dataclass
class ClassifiedFile:
    path: Path
    relative_path: str
    category: FileCategory
    confidence: float
    reasons: list[str] = field(default_factory=list)
    sheet_number: str | None = None
    size_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "relative_path": self.relative_path,
            "category": self.category.value,
            "confidence": round(self.confidence, 3),
            "reasons": self.reasons,
            "sheet_number": self.sheet_number,
            "size_bytes": self.size_bytes,
        }


def should_skip(path: Path) -> bool:
    parts_lower = [p.lower() for p in path.parts]
    name_lower = path.name.lower()
    if name_lower.startswith("~$"):
        return True
    if name_lower.startswith("."):
        return True
    return any(part in SKIP_NAME_PARTS for part in parts_lower) or name_lower in SKIP_NAME_PARTS


def _normalize(text: str) -> str:
    return re.sub(r"[_\-]+", " ", text).lower()


def _count_hits(haystack: str, needles: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for needle in needles:
        if needle and needle.lower() in haystack:
            hits.append(needle)
    return hits


def _folder_text(relative_path: str) -> str:
    parent = str(Path(relative_path).parent)
    if parent in (".", ""):
        return ""
    return _normalize(parent)


def _peek_pdf_text(path: Path, max_chars: int = 2500) -> str:
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        import fitz  # type: ignore
    except ImportError:
        return ""
    try:
        doc = fitz.open(path)
        try:
            if doc.page_count < 1:
                return ""
            return (doc.load_page(0).get_text("text") or "")[:max_chars]
        finally:
            doc.close()
    except Exception:
        return ""


def _score_category(
    category: FileCategory,
    hints: CategoryHints,
    *,
    folder_blob: str,
    name_blob: str,
    text_blob: str,
    suffix: str,
    sheet_hit: str | None,
    csi_hit: bool,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    folder_hits = _count_hits(folder_blob, hints.folders)
    if folder_hits:
        score += 4 * min(len(folder_hits), 2)
        reasons.append(f"folder hints: {', '.join(folder_hits[:4])}")

    name_hits = _count_hits(name_blob, hints.filenames)
    if name_hits:
        score += 3 * min(len(name_hits), 2)
        reasons.append(f"filename hints: {', '.join(name_hits[:4])}")

    if text_blob:
        text_hits = _count_hits(text_blob.lower(), hints.text)
        if text_hits:
            score += 2
            reasons.append(f"content hints: {', '.join(text_hits[:3])}")

    if category == FileCategory.DRAWING:
        if sheet_hit:
            score += 5
            reasons.append(f"sheet number {sheet_hit}")
        if suffix in {".dwg", ".dxf"}:
            score += 5
            reasons.append(f"{suffix} CAD file")
        elif suffix in IMAGE_EXTENSIONS and (sheet_hit or folder_hits):
            score += 2
            reasons.append("image in a drawing-like location")

    if category == FileCategory.SPEC and csi_hit:
        score += 3
        reasons.append("CSI section number in name")

    if category == FileCategory.SCHEDULE and suffix in {".mpp", ".xer", ".xml"}:
        score += 4
        reasons.append(f"{suffix} schedule file")

    return score, reasons


def classify_file(
    path: Path,
    *,
    root: Path | None = None,
    hints: dict[FileCategory, CategoryHints] | None = None,
    peek_pdf: bool = False,
) -> ClassifiedFile | None:
    """Classify one file. Returns None for junk / hidden / lock files."""
    path = path.resolve()
    if not path.is_file() or should_skip(path):
        return None

    root = root.resolve() if root is not None else path.parent
    try:
        relative = str(path.relative_to(root))
    except ValueError:
        relative = path.name

    hint_map = hints or DEFAULT_HINTS
    folder_blob = _folder_text(relative)
    name_blob = _normalize(path.stem)
    suffix = path.suffix.lower()
    sheet_match = SHEET_NUMBER_RE.search(path.stem)
    sheet_hit = sheet_match.group(0).upper() if sheet_match else None
    csi_hit = bool(CSI_SECTION_RE.search(path.stem) or CSI_SECTION_RE.search(name_blob))
    text_blob = _peek_pdf_text(path) if peek_pdf else ""

    best_category = FileCategory.OTHER
    best_score = 0
    best_reasons: list[str] = []

    for category, category_hints in hint_map.items():
        score, reasons = _score_category(
            category,
            category_hints,
            folder_blob=folder_blob,
            name_blob=name_blob,
            text_blob=text_blob,
            suffix=suffix,
            sheet_hit=sheet_hit,
            csi_hit=csi_hit,
        )
        if score > best_score:
            best_score = score
            best_category = category
            best_reasons = reasons

    # Sheet numbers identify drawings even when the file sits in a Bid / ITB
    # folder (GCs often dump the entire set there).
    if sheet_hit and suffix in DRAWING_EXTENSIONS:
        if best_category != FileCategory.DRAWING or best_score < 5:
            best_category = FileCategory.DRAWING
            best_score = max(best_score, 6)
            if f"sheet number {sheet_hit}" not in best_reasons:
                best_reasons = [f"sheet number {sheet_hit}", *best_reasons]

    if best_score == 0 and suffix not in DRAWING_EXTENSIONS | DOCUMENT_EXTENSIONS | {".mpp", ".xer"}:
        best_reasons = ["unrecognized file type"]
    elif best_score == 0:
        best_reasons = ["no category signals"]

    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0

    confidence = min(1.0, best_score / 8.0) if best_score else 0.15
    return ClassifiedFile(
        path=path,
        relative_path=relative.replace("\\", "/"),
        category=best_category,
        confidence=confidence,
        reasons=best_reasons,
        sheet_number=sheet_hit if best_category == FileCategory.DRAWING else None,
        size_bytes=size_bytes,
    )


def classify_files(
    files: Iterable[Path],
    *,
    root: Path,
    hints: dict[FileCategory, CategoryHints] | None = None,
    peek_pdf: bool = False,
) -> list[ClassifiedFile]:
    classified: list[ClassifiedFile] = []
    for path in files:
        item = classify_file(path, root=root, hints=hints, peek_pdf=peek_pdf)
        if item is not None:
            classified.append(item)
    return classified
