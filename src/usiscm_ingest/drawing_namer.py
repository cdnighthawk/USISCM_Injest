"""Automatic drawing names — the desktop namer without a person at the keyboard.

Mirrors USISPdfApp / CM_Deploy ``drawing_label`` + hygiene so sheet numbers,
titles, discipline, set, and revision come from the sheet itself (title-block
text after a page split) and, for an already-single sheet, the filename and
folder. Ambiguous names still ingest; they are flagged for the website
ingest-error queue. Package and form tokens (PKG1, ADD01, NO.4, W9) are not
sheet numbers.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LABEL_OK = "ok"
LABEL_NEEDS_AI = "needs_ai"
LABEL_UNKNOWN = "unknown"

_SHEET_NUM_RE = re.compile(
    r"^(?:[A-Z]{1,3}\d{0,2}-)?[A-Z]{1,3}[-\s.]?\d{1,4}(?:[.\-]\d{1,4}){0,3}(?:-[A-Z0-9]{1,3})?[A-Z]?$",
    re.IGNORECASE,
)
_REV = re.compile(r"(?:^|[_-])rev(?:ision)?[-_]?([A-Z0-9.]+)", re.IGNORECASE)
_PAGE_RE = re.compile(r"^(?:page|sheet|pg)[\s._-]*\d+$", re.IGNORECASE)
_IMG_RE = re.compile(r"^(img|image|dsc|scan)[_-]?\d+", re.IGNORECASE)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Tokens that look like sheet ids but come from package names, addenda, and forms.
# ADD01, PKG1, NO.4, and W9 must not become drawing numbers.
_JUNK_SHEET_PREFIXES = {
    "ADD",
    "ADDM",
    "ASI",
    "BID",
    "DIV",
    "DOC",
    "FORM",
    "ITB",
    "NO",
    "PK",
    "PKG",
    "RFP",
    "RFQ",
    "SEC",
    "SPEC",
    "VOL",
}

_SHEET_FIND_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"((?:[A-Z]{1,3}\d{0,2}-)?[A-Z]{1,3}[-\s.]?\d{1,4}(?:[.\-]\d{1,4}){0,3}(?:-[A-Z0-9]{1,3})?[A-Z]?)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_LABELED_SHEET_RE = re.compile(
    r"(?:sheet|drawing|dwg)\.?\s*(?:no\.?|number|#)\s*[:\-]?\s*"
    r"([A-Z]{1,3}(?:\d{0,2}-)?[A-Z]{0,3}[-.\s]?\d{1,4}(?:[.\-]\d{1,4}){0,3}(?:-[A-Z0-9]{1,3})?[A-Z]?)",
    re.IGNORECASE,
)
_LABELED_TITLE_RE = re.compile(
    r"(?:sheet\s*title|drawing\s*title)\s*[:\-]\s*([^\n\r]{2,160})",
    re.IGNORECASE,
)
_TITLE_NOISE_RE = re.compile(
    r"^(?:scale|date|drawn|checked|check|project|revision|rev\.?|sheet|drawing|dwg|"
    r"no\.?|north|copyright|not for construction|plot|file|job|consultant|"
    r"architect|engineer|stamp|seal)\b",
    re.IGNORECASE,
)

_DISC_FROM_PREFIX: tuple[tuple[tuple[str, ...], str], ...] = (
    (("ID", "AD", "I", "A"), "Architectural"),
    (("S",), "Structural"),
    (("MP", "MD", "M"), "Mechanical"),
    (("EL", "EP", "E"), "Electrical"),
    (("PL", "P"), "Plumbing"),
    (("CG", "CS", "C"), "Civil"),
    (("LA", "LS", "L"), "Landscape"),
    (("FA", "FP", "F"), "Fire Protection"),
    (("G",), "General"),
    (("T",), "Telecom"),
)

_FOLDER_DISC_ALIASES = {
    "arch": "Architectural",
    "architectural": "Architectural",
    "architecture": "Architectural",
    "05-architectural": "Architectural",
    "struct": "Structural",
    "structural": "Structural",
    "mech": "Mechanical",
    "mechanical": "Mechanical",
    "elec": "Electrical",
    "electrical": "Electrical",
    "plumb": "Plumbing",
    "plumbing": "Plumbing",
    "civil": "Civil",
    "landscape": "Landscape",
    "fire": "Fire Protection",
    "fire-protection": "Fire Protection",
    "general": "General",
    "01-general": "General",
    "telecom": "Telecom",
    "interiors": "Interiors",
}


@dataclass
class DrawingName:
    sheet_number: str | None
    sheet_title: str | None
    discipline: str | None
    drawing_set: str | None
    revision: str
    confidence: float
    needs_review: bool
    label_status: str
    reasons: list[str] = field(default_factory=list)
    filename_guess: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_number": self.sheet_number,
            "sheet_title": self.sheet_title,
            "discipline": self.discipline,
            "drawing_set": self.drawing_set,
            "revision": self.revision,
            "confidence": round(self.confidence, 3),
            "needs_review": self.needs_review,
            "label_status": self.label_status,
            "reasons": self.reasons,
            "filename_guess": self.filename_guess,
        }

    def review_message(self) -> str:
        if not self.needs_review:
            return ""
        if self.reasons:
            return "; ".join(self.reasons)
        if not self.sheet_number:
            return "drawing # could not be read automatically — review the name on the website"
        return "automatic drawing name needs review"


def _leading_sheet_letters(token: str) -> str:
    raw = re.sub(r"[^A-Z0-9.\-]", "", (token or "").upper())
    package = re.match(r"^P\d+-", raw)
    if package:
        raw = raw[package.end() :]
    match = re.match(r"^([A-Z]+)", raw)
    return match.group(1) if match else ""


def is_junk_sheet_token(raw: str | None) -> bool:
    """Package, addendum, and form tokens that must not be stored as sheet numbers."""
    token = (raw or "").strip()
    if not token:
        return False
    compact = re.sub(r"[^A-Z0-9]", "", token.upper())
    if compact in {"W9", "W9FORM", "FW9"}:
        return True
    if re.fullmatch(r"W[-.\s]?9", token, re.IGNORECASE):
        return True
    return _leading_sheet_letters(token) in _JUNK_SHEET_PREFIXES


def is_sheet_number(raw: str | None) -> bool:
    token = (raw or "").strip()
    if not token or _PAGE_RE.match(token):
        return False
    if is_junk_sheet_token(token):
        return False
    return bool(_SHEET_NUM_RE.match(token))


def find_sheet_number(text: str | None) -> str | None:
    """First real sheet id in ``text``. Skips ADD01, PKG1, NO.4, W9, and similar."""
    raw = text or ""
    for match in _SHEET_FIND_RE.finditer(raw):
        token = normalize_sheet_number(match.group(1))
        if token and is_sheet_number(token):
            return token
    return None


def normalize_sheet_number(raw: str | None) -> str | None:
    token = (raw or "").strip()
    if not token:
        return None
    return token.upper().replace(" ", "")[:50]


def _title_from_rest(rest: str) -> str | None:
    parts = [p for p in re.split(r"[_]+", rest) if p]
    keep: list[str] = []
    for part in parts:
        if _REV.match(part) or re.match(r"^rev[-_]?", part, re.I):
            continue
        keep.append(part.replace("-", " ").strip())
    title = " ".join(keep).strip()
    return title[:500] or None


def parse_filename(filename: str | None) -> dict[str, str | None]:
    name = Path(filename or "").name
    stem = name.rsplit(".", 1)[0] if name else ""
    if not stem:
        return {"sheet_number": None, "sheet_title": None, "revision": None}

    token, sep, rest = stem.partition("_")
    if not sep:
        bits = stem.split(None, 1)
        token = bits[0] if bits else ""
        rest = bits[1] if len(bits) > 1 else ""
    token = token.strip()
    sheet = None
    title = None
    if token and is_sheet_number(token):
        sheet = normalize_sheet_number(token)
        if rest:
            title = _title_from_rest(rest)
    if sheet is None:
        compact = stem.replace("_", " ").replace("—", " ")
        for match in _SHEET_FIND_RE.finditer(compact):
            candidate = normalize_sheet_number(match.group(1))
            if not candidate or not is_sheet_number(candidate):
                continue
            sheet = candidate
            leftover = f"{compact[: match.start()]} {compact[match.end() :]}".strip(" -_")
            title = leftover[:500] or None
            break

    rev = None
    match = _REV.search(stem)
    if match:
        rev = match.group(1)[:50]
    return {"sheet_number": sheet, "sheet_title": title, "revision": rev}


def discipline_from_sheet_number(sheet_number: str | None) -> str | None:
    raw = (sheet_number or "").strip().upper()
    if not raw:
        return None
    if raw.startswith("P") and "-" in raw:
        raw = raw.split("-", 1)[1]
    letters = re.match(r"^([A-Z]{1,3})", raw)
    if not letters:
        return None
    prefix = letters.group(1)
    for keys, name in _DISC_FROM_PREFIX:
        if prefix in keys:
            return name
    return None


def normalize_discipline(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    alias = _FOLDER_DISC_ALIASES.get(text.lower())
    if alias:
        return alias
    return text[:50]


def parse_folder_path(path: str | None) -> dict[str, str | None]:
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p and p not in (".", "..")]
    if parts and parts[0].lower() == "drawings":
        parts = parts[1:]
    if len(parts) < 1:
        return {"job": None, "discipline": None, "drawing_set": None, "filename": None}
    filename = parts[-1] if "." in parts[-1] else None
    segs = parts[:-1] if filename else parts
    job = None
    discipline = None
    drawing_set = None
    if segs and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", segs[0] or ""):
        if re.fullmatch(r"\d{4,8}", segs[0]) or re.fullmatch(r"[A-Z]{0,3}\d{4,8}", segs[0], re.I):
            job = segs[0]
            segs = segs[1:]
    if segs:
        discipline = normalize_discipline(segs[0])
    if len(segs) >= 2:
        drawing_set = segs[1][:120]
    return {
        "job": job,
        "discipline": discipline,
        "drawing_set": drawing_set,
        "filename": filename,
    }


def _digits(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


def classify_label(sheet_number: str | None, filename: str | None = None) -> dict[str, Any]:
    raw = (sheet_number or "").strip()
    from_file = parse_filename(filename).get("sheet_number")
    if raw and is_sheet_number(raw) and not _PAGE_RE.match(raw):
        conflict = bool(from_file) and _digits(raw) != _digits(from_file)
        if conflict:
            return {
                "label_status": LABEL_NEEDS_AI,
                "label_reason": "filename sheet number disagrees with stored drawing #",
                "filename_guess": from_file,
            }
        return {
            "label_status": LABEL_OK,
            "label_reason": "matches drawing-number pattern",
            "filename_guess": from_file,
        }
    if raw and (_PAGE_RE.match(raw) or _IMG_RE.match(raw) or _UUID_RE.match(raw)):
        return {
            "label_status": LABEL_NEEDS_AI,
            "label_reason": "drawing # looks like a page, image, or id — not A-100 style",
            "filename_guess": from_file,
        }
    if from_file and is_sheet_number(from_file):
        return {
            "label_status": LABEL_NEEDS_AI,
            "label_reason": "drawing # is missing or nonstandard; filename looks like a sheet id",
            "filename_guess": from_file,
        }
    if not raw:
        return {
            "label_status": LABEL_UNKNOWN,
            "label_reason": "no drawing # and no filename pattern",
            "filename_guess": None,
        }
    return {
        "label_status": LABEL_NEEDS_AI,
        "label_reason": "drawing # does not match A-100 / A2.01 style",
        "filename_guess": from_file,
    }


def _clean_title(raw: str) -> str | None:
    title = re.sub(r"\s+", " ", raw or "").strip(" \t-_:|")
    title = re.sub(r"\s+(?:scale|date|rev(?:ision)?)\b.*$", "", title, flags=re.I).strip()
    if len(title) < 2:
        return None
    if is_sheet_number(title) or is_junk_sheet_token(title):
        return None
    return title[:500]


def _looks_like_title(line: str) -> bool:
    text = line.strip()
    if len(text) < 3 or len(text) > 120 or not re.search(r"[A-Za-z]", text):
        return False
    if _TITLE_NOISE_RE.match(text) or _PAGE_RE.match(text):
        return False
    if is_sheet_number(text) or is_junk_sheet_token(text):
        return False
    return True


def sheet_number_from_page_text(text: str | None) -> str | None:
    """Sheet id from one page, preferring a labeled title-block number."""
    raw = text or ""
    if not raw.strip():
        return None
    labeled: list[str] = []
    for match in _LABELED_SHEET_RE.finditer(raw):
        token = normalize_sheet_number(match.group(1))
        if token and is_sheet_number(token):
            labeled.append(token)
    if labeled:
        return labeled[-1]
    tail = raw[-2500:]
    found: list[str] = []
    for match in _SHEET_FIND_RE.finditer(tail):
        token = normalize_sheet_number(match.group(1))
        if token and is_sheet_number(token):
            found.append(token)
    if found:
        return found[-1]
    return None


def identity_from_page_text(text: str | None) -> dict[str, str | None]:
    """Sheet number and title read from a single page's text."""
    raw = text or ""
    number = sheet_number_from_page_text(raw)
    title = None
    labeled = _LABELED_TITLE_RE.search(raw)
    if labeled:
        title = _clean_title(labeled.group(1))
    if title is None and number:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        needle = number.upper().replace(" ", "")
        index = None
        for i, line in enumerate(lines):
            compact = re.sub(r"[^A-Z0-9.\-]", "", line.upper())
            if needle and needle in compact:
                index = i
        if index is not None:
            for line in reversed(lines[max(0, index - 5) : index]):
                if _looks_like_title(line):
                    title = _clean_title(line)
                    if title:
                        break
    return {"sheet_number": number, "sheet_title": title}


def name_drawing(
    *,
    filename: str | None,
    folder_path: str | None = None,
    sheet_number: str | None = None,
    sheet_title: str | None = None,
    discipline: str | None = None,
    drawing_set: str | None = None,
    revision: str | None = None,
    page_text: str | None = None,
    use_filename_sheet: bool = True,
    use_filename_title: bool = True,
) -> DrawingName:
    """Fill sheet labels from the page, then the path. Never waits for a person.

    Multi-page parents must pass ``use_filename_sheet=False`` so a fat filename
    cannot stamp PKG1 / ADD01 / NO.4 / W9 onto every sheet. After a split, pass
    that page's text and let the title block win.
    """
    parsed = parse_filename(filename)
    from_page = identity_from_page_text(page_text)
    folder = (
        parse_folder_path(folder_path)
        if folder_path
        else {"job": None, "discipline": None, "drawing_set": None, "filename": None}
    )
    sn = (sheet_number or "").strip() or None
    if sn and not is_sheet_number(sn):
        sn = None
    if not sn and use_filename_sheet:
        file_sheet = parsed["sheet_number"]
        if file_sheet and is_sheet_number(str(file_sheet)):
            sn = str(file_sheet)
    if not sn and from_page.get("sheet_number"):
        sn = from_page["sheet_number"]
    title = (sheet_title or "").strip() or None
    if not title and from_page.get("sheet_title"):
        title = from_page["sheet_title"]
    if not title and use_filename_title:
        title = parsed["sheet_title"]
    disc = normalize_discipline(discipline) or folder["discipline"]
    if not disc and sn:
        disc = discipline_from_sheet_number(sn)
    dset = (drawing_set or "").strip() or None
    if not dset:
        dset = folder["drawing_set"]
    rev = (revision or "").strip() or None
    if (not rev or rev == "0") and parsed["revision"]:
        rev = parsed["revision"]
    if not rev:
        rev = "0"
    if sn and not _PAGE_RE.match(sn):
        sn = normalize_sheet_number(sn)

    hygiene = classify_label(sn, filename if use_filename_sheet else None)
    reasons: list[str] = []
    if hygiene.get("label_reason"):
        if hygiene["label_status"] != LABEL_OK:
            reasons.append(str(hygiene["label_reason"]))
    if not title and use_filename_title:
        reasons.append("sheet title missing — used filename stem")
        stem = Path(filename or "").stem
        title = stem.replace("_", " ").replace("-", " ").strip()[:500] or None
    elif not title:
        reasons.append("sheet title missing")
    needs_review = hygiene["label_status"] != LABEL_OK or not sn or not title
    if hygiene["label_status"] == LABEL_OK and title:
        confidence = 0.92
    elif hygiene["label_status"] == LABEL_NEEDS_AI:
        confidence = 0.55
    else:
        confidence = 0.25
    return DrawingName(
        sheet_number=sn,
        sheet_title=(title[:500] if title else None),
        discipline=(disc[:50] if disc else None),
        drawing_set=(dset[:120] if dset else None),
        revision=(rev[:50] if rev else "0"),
        confidence=confidence,
        needs_review=needs_review,
        label_status=str(hygiene["label_status"]),
        reasons=reasons,
        filename_guess=hygiene.get("filename_guess"),
    )


def title_block_jpeg_base64(path: Path, *, dpi: int = 72) -> str | None:
    """Optional bottom-right crop of page 1 for the website sheet-identity AI."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    try:
        doc = fitz.open(path)
    except Exception as exc:
        logger.debug("could not open PDF for title-block crop %s: %s", path, exc)
        return None
    try:
        if doc.page_count < 1:
            return None
        page = doc[0]
        pix = page.get_pixmap(dpi=max(36, min(dpi, 120)), alpha=False)
        width, height = pix.width, pix.height
        x0 = int(width * 0.55)
        y0 = int(height * 0.62)
        clip = fitz.IRect(x0, y0, width, height)
        cropped = fitz.Pixmap(pix, clip)
        return base64.b64encode(cropped.tobytes("jpeg")).decode("ascii")
    except Exception as exc:
        logger.debug("title-block crop failed %s: %s", path, exc)
        return None
    finally:
        doc.close()


def apply_ai_identity(named: DrawingName, identity: dict[str, Any] | None) -> DrawingName:
    """Merge a website ``/ai/sheet-identity`` item onto an automatic name."""
    if not isinstance(identity, dict):
        return named
    number = str(identity.get("sheetNumber") or identity.get("sheet_number") or "").strip() or None
    title = str(identity.get("sheetTitle") or identity.get("sheet_title") or "").strip() or None
    rev = str(identity.get("revisionLabel") or identity.get("revision_label") or "").strip() or None
    try:
        conf = float(identity.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    flagged = identity.get("needsReview") if "needsReview" in identity else identity.get("needs_review")
    sheet = named.sheet_number
    reasons = list(named.reasons)
    if number and is_sheet_number(number):
        sheet = normalize_sheet_number(number)
    elif number:
        reasons.append(f"AI sheet number {number!r} is not A-100 style")
    new_title = title or named.sheet_title
    new_rev = rev or named.revision
    disc = named.discipline
    if sheet and not disc:
        disc = discipline_from_sheet_number(sheet)
    hygiene = classify_label(sheet, None)
    if hygiene["label_status"] != LABEL_OK and hygiene.get("label_reason"):
        reasons.append(str(hygiene["label_reason"]))
    confident = (
        bool(sheet)
        and bool(new_title)
        and conf >= 0.80
        and hygiene["label_status"] == LABEL_OK
        and not bool(flagged)
        and is_sheet_number(sheet)
    )
    return DrawingName(
        sheet_number=sheet,
        sheet_title=new_title,
        discipline=disc,
        drawing_set=named.drawing_set,
        revision=(new_rev[:50] if new_rev else "0"),
        confidence=max(named.confidence, min(1.0, max(0.0, conf))),
        needs_review=not confident,
        label_status=str(hygiene["label_status"]),
        reasons=reasons if not confident else [],
        filename_guess=named.filename_guess,
    )
