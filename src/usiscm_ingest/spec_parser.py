"""Split Specs / project manuals into CSI section PDFs with Spec_Parser.

Drawing sheet-split is unchanged. This step runs only after a PDF is already
on the documents path as a spec. Section PDFs are written under the estimate
folder (``02_Processed\\spec_splits\\<stem>``). They are documents on disk.
They are never sheet-split and never sent to the Drawings API.

The CLI is Charles's existing tool. This module does not reimplement parsing.

Default install: ``D:\\Programs\\Spec_Parser`` (override with ``SPEC_PARSER_DIR``
or ``USISCM_SPEC_PARSER_DIR``).

    python cli.py <Specifications.pdf> -o <outdir> --by section

Working directory is the Spec_Parser directory. ``gui.py`` is never used.

A missing install or a parse error is logged and ignored. The whole Specs PDF
stays a document and the rest of ingest continues.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from usiscm_ingest.classify import FileCategory

logger = logging.getLogger(__name__)

DEFAULT_SPEC_PARSER_DIR = Path(r"D:\Programs\Spec_Parser")
PROCESSED_SPEC_SPLITS = Path("02_Processed") / "spec_splits"

# Huge-spec chunking. Same thresholds BidDocProcessor uses.
_SPEC_CHUNK_PAGES = 800
_SPEC_CHUNK_MB = 150
_SPEC_CHUNK_SIZE = 300  # pages per chunk

_CLI_NAMES = ("cli.py", "main.py", "run.py", "app.py")

_NON_MANUAL_RE = re.compile(
    r"(?i)(notice\s+inviting|\bNIB\b|invitation\s+to\s+bid|"
    r"bid\s+and\s+contract|instructions?\s+to\s+bidders?|"
    r"general\s+provisions|special\s+provisions|"
    r"door\s+hardware|hardware\s+sets|addendum\s+letter)"
)
_MANUAL_NAME_RE = re.compile(
    r"(?i)(specification|project\s+manual|spec\s+manual|\bspecs?\b|div(ision)?\s*\d{2})"
)
_CSI_SECTION_RE = re.compile(r"^\d{2}[\s._-]*\d{2}[\s._-]*\d{2}\b")
_CSI_FLAT_RE = re.compile(r"^\d{6}\b")


def spec_parser_dir() -> Path:
    """Install directory. ``SPEC_PARSER_DIR`` wins, then ``USISCM_SPEC_PARSER_DIR``."""
    for name in ("SPEC_PARSER_DIR", "USISCM_SPEC_PARSER_DIR"):
        raw = os.environ.get(name, "").strip().strip('"')
        if raw:
            return Path(raw).expanduser()
    return DEFAULT_SPEC_PARSER_DIR


def spec_parser_python() -> str:
    """Interpreter for ``cli.py``.

    ``SPEC_PARSER_PYTHON`` (or ``USISCM_SPEC_PARSER_PYTHON``) when Spec_Parser's
    dependencies are not installed in the ingest environment. Otherwise this
    process, which is the ingest venv.
    """
    for name in ("SPEC_PARSER_PYTHON", "USISCM_SPEC_PARSER_PYTHON"):
        raw = os.environ.get(name, "").strip().strip('"')
        if raw:
            return raw
    return sys.executable


def spec_split_dir(estimate_folder: Path, pdf: Path) -> Path:
    """``<estimate>\\02_Processed\\spec_splits\\<stem>`` — never ``drawings``."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", pdf.stem).strip(" ._")
    stem = (stem[:60] or "spec")
    return Path(estimate_folder) / PROCESSED_SPEC_SPLITS / stem


def _probe_pdf_kind(pdf: Path) -> tuple[str, str]:
    """Return (kind, reason) where kind is drawings|specs|mixed|unknown.

    Cheap checks only: page geometry on the first few pages plus a little text.
    Same idea as BidDocProcessor — letter-size CSI books are specs; large
    landscape sheets are drawings.
    """
    try:
        import pymupdf
    except ImportError:
        return "unknown", "pymupdf missing"
    try:
        doc = pymupdf.open(pdf)
    except Exception as exc:
        return "unknown", f"cannot open: {exc}"
    try:
        n = doc.page_count
        if n < 1:
            return "unknown", "empty pdf"
        sample = min(n, 3)
        large = 0
        letterish = 0
        landscape = 0
        csi_hits = 0
        text_chars = 0
        for i in range(sample):
            page = doc.load_page(i)
            rect = page.rect
            w, h = float(rect.width), float(rect.height)
            short, long_ = (w, h) if w < h else (h, w)
            if long_ >= 1224 and short >= 700:
                large += 1
            elif max(w, h) >= 1440:
                large += 1
            if 580 <= short <= 650 and 740 <= long_ <= 1100:
                letterish += 1
            if w > h * 1.05:
                landscape += 1
            try:
                txt = page.get_text("text") or ""
            except Exception:
                txt = ""
            if len(txt) > 6000:
                txt = txt[:6000]
            text_chars += len(txt)
            if re.search(
                r"(?i)(SECTION\s+\d{2}\s*\d{2}\s*\d{2}|PART\s+[123]\s*[-—]|"
                r"DIVISION\s+\d{2}|MasterFormat|\bCSI\b)",
                txt,
            ):
                csi_hits += 1
        if large >= max(1, sample // 2) and landscape >= 1:
            return "drawings", f"large-format pages ({large}/{sample}), landscape"
        if large >= sample:
            return "drawings", f"large-format pages ({large}/{sample})"
        if csi_hits >= 1 and letterish >= max(1, sample // 2):
            return "specs", f"CSI text + letter-size ({letterish}/{sample})"
        if letterish >= sample and text_chars / max(sample, 1) > 800 and large == 0:
            return "specs", f"letter-size text-heavy ({text_chars} chars / {sample}p)"
        if large >= 1 and letterish == 0:
            return "drawings", "has large-format page"
        return "unknown", f"pages={n} large={large} letter={letterish} csi={csi_hits}"
    finally:
        doc.close()


def _should_spec_parse(pdf: Path) -> tuple[bool, str]:
    """Manuals and CSI books only. Skip NIB, bid admin, hardware, and section files.

    Callers already limited this to spec documents. ``unknown`` geometry is
    allowed so a manual whose first pages lack CSI text still parses.
    """
    name = pdf.name
    kind, why = _probe_pdf_kind(pdf)
    if kind == "drawings":
        return False, f"skip — PDF looks like drawings ({why}): {name}"
    if _NON_MANUAL_RE.search(name):
        return False, f"skip non-manual for Spec_Parser: {name}"
    if _MANUAL_NAME_RE.search(name):
        return True, ""
    stem = pdf.stem.strip()
    if _CSI_SECTION_RE.match(stem) or _CSI_FLAT_RE.match(stem):
        return False, f"skip CSI section file (already a section): {name}"
    if kind == "unknown":
        return True, ""
    if kind == "specs":
        return True, ""
    return False, f"skip — PDF type unconfirmed for Spec_Parser ({why}): {name}"


def _pdf_page_count(pdf: Path) -> int:
    try:
        import pymupdf

        doc = pymupdf.open(pdf)
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except Exception:
        return 0


def _spec_pdf_stats(pdf: Path) -> tuple[int, float]:
    size_mb = pdf.stat().st_size / (1024 * 1024)
    return _pdf_page_count(pdf), size_mb


def _section_dedupe_key(pdf_path: Path) -> str:
    stem = pdf_path.stem.strip()
    match = re.match(r"^(\d{2})[\s._-]*(\d{2})[\s._-]*(\d{2})", stem)
    if match:
        return f"csi:{match.group(1)}{match.group(2)}{match.group(3)}"
    return "name:" + re.sub(r"[^a-z0-9]+", "", stem.lower())[:80]


def _merge_spec_chunk_outputs(chunk_dirs: list[Path], dest: Path) -> int:
    """Copy unique section PDFs from chunk dirs into dest. Returns count written."""
    dest.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    written = 0
    for chunk_dir in chunk_dirs:
        if not chunk_dir.is_dir():
            continue
        for src in sorted(chunk_dir.rglob("*.pdf")):
            key = _section_dedupe_key(src)
            if key in seen:
                continue
            seen.add(key)
            target = dest / src.name
            if target.exists():
                stem, suf = target.stem, target.suffix
                n = 2
                while target.exists():
                    target = dest / f"{stem}_{n}{suf}"
                    n += 1
            shutil.copy2(src, target)
            written += 1
    return written


def _chunk_pdf_pages(pdf: Path, chunk_dir: Path, chunk_pages: int = _SPEC_CHUNK_SIZE) -> list[Path]:
    """Split pdf into page-range chunks. Returns chunk file paths."""
    import pymupdf

    chunk_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf)
    chunks: list[Path] = []
    try:
        n = doc.page_count
        for start in range(0, n, chunk_pages):
            end = min(start + chunk_pages, n) - 1
            out = chunk_dir / f"chunk_{start + 1:04d}_{end + 1:04d}.pdf"
            one = pymupdf.open()
            one.insert_pdf(doc, from_page=start, to_page=end)
            one.save(out, garbage=1, deflate=True)
            one.close()
            chunks.append(out)
    finally:
        doc.close()
    return chunks


def _find_spec_parser_cli(parser_dir: Path | None = None) -> Path | None:
    root = parser_dir if parser_dir is not None else spec_parser_dir()
    if not root.is_dir():
        return None
    for name in _CLI_NAMES:
        cand = root / name
        if cand.is_file():
            return cand
    return None


def _run_spec_parser_once(
    cli: Path,
    pdf: Path,
    out: Path,
    *,
    progress=None,
    pct: float = 0,
    timeout: int = 900,
) -> dict:
    """Invoke Spec_Parser on one PDF (a manual or one chunk of a manual)."""
    out.mkdir(parents=True, exist_ok=True)
    parser_dir = spec_parser_dir()
    cmd = [spec_parser_python(), str(cli), str(pdf), "-o", str(out), "--by", "section"]
    logger.info("Spec_Parser command: %s (cwd %s)", " ".join(cmd), parser_dir)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(parser_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:
        return {"pdf": str(pdf), "ok": False, "error": str(exc), "out": str(out)}

    started = time.time()
    last_count = -1
    last_beat = started
    try:
        while True:
            rc = proc.poll()
            now = time.time()
            elapsed = int(now - started)
            if now - last_beat >= 10.0:
                count = sum(1 for _ in out.rglob("*.pdf")) if out.is_dir() else 0
                message = f"Spec_Parser working… {count} section PDF(s), {elapsed}s elapsed ({pdf.name})"
                if progress and count != last_count:
                    progress(message, pct)
                elif count != last_count:
                    logger.info(message)
                last_count = count
                last_beat = now
            if rc is not None:
                break
            if elapsed > timeout:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                return {
                    "pdf": str(pdf),
                    "ok": False,
                    "error": f"timed out after {timeout}s",
                    "out": str(out),
                }
            time.sleep(0.25)
        stdout, stderr = proc.communicate(timeout=30)
        err_tail = (stderr or "")[-800:]
        out_tail = (stdout or "")[-800:]
        ok = proc.returncode == 0
        result = {
            "pdf": str(pdf),
            "ok": ok,
            "out": str(out),
            "stdout_tail": out_tail,
            "stderr_tail": err_tail,
        }
        if not ok:
            result["error"] = (err_tail or out_tail or f"exit {proc.returncode}").strip()[:400]
        return result
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        return {"pdf": str(pdf), "ok": False, "error": str(exc), "out": str(out)}


def _run_spec_parser(pdf: Path, out: Path, *, progress=None, pct: float = 0) -> dict:
    """Run Spec_Parser. Chunk manuals over ~800 pages or ~150 MB."""
    cli = _find_spec_parser_cli()
    if cli is None:
        return {
            "pdf": str(pdf),
            "ok": False,
            "error": f"Spec_Parser not found at {spec_parser_dir()}",
        }

    try:
        pages, size_mb = _spec_pdf_stats(pdf)
    except Exception as exc:
        return {"pdf": str(pdf), "ok": False, "error": str(exc), "out": str(out)}

    needs_chunk = pages > _SPEC_CHUNK_PAGES or size_mb > _SPEC_CHUNK_MB
    if not needs_chunk:
        return _run_spec_parser_once(cli, pdf, out, progress=progress, pct=pct)

    logger.info(
        "Large manual %s (%s pages, %.0f MB) — chunking for Spec_Parser (~%s pages/chunk)",
        pdf.name,
        pages,
        size_mb,
        _SPEC_CHUNK_SIZE,
    )
    if progress:
        progress(
            f"Large manual ({pages} pages, {size_mb:.0f} MB) — chunking for Spec_Parser",
            pct,
        )

    tmp_root = Path(tempfile.mkdtemp(prefix="spec_chunks_"))
    try:
        try:
            chunks = _chunk_pdf_pages(pdf, tmp_root / "pdfs", _SPEC_CHUNK_SIZE)
        except Exception as exc:
            logger.warning("Chunking failed for %s (%s); keeping the whole PDF as a document", pdf.name, exc)
            return {
                "pdf": str(pdf),
                "ok": False,
                "error": f"too large to parse ({pages}p / {size_mb:.0f}MB); chunk failed: {exc}",
                "out": str(out),
                "pages": pages,
                "size_mb": size_mb,
            }

        chunk_out_dirs: list[Path] = []
        errors: list[str] = []
        stderr_bits: list[str] = []
        any_ok = False
        for i, chunk_pdf in enumerate(chunks):
            cdest = tmp_root / "out" / f"chunk_{i:03d}"
            logger.info("Spec_Parser chunk %s/%s: %s", i + 1, len(chunks), chunk_pdf.name)
            if progress:
                progress(f"Spec_Parser chunk {i + 1}/{len(chunks)}: {chunk_pdf.name}", pct)
            one = _run_spec_parser_once(cli, chunk_pdf, cdest, progress=progress, pct=pct, timeout=600)
            chunk_out_dirs.append(cdest)
            if one.get("ok"):
                any_ok = True
            else:
                errors.append(f"chunk {i + 1}: {one.get('error') or 'fail'}")
            if one.get("stderr_tail"):
                stderr_bits.append(one["stderr_tail"])

        n_written = _merge_spec_chunk_outputs(chunk_out_dirs, out)
        ok = any_ok and n_written > 0
        result = {
            "pdf": str(pdf),
            "ok": ok,
            "out": str(out),
            "chunked": True,
            "chunks": len(chunks),
            "sections_merged": n_written,
            "pages": pages,
            "size_mb": size_mb,
            "stderr_tail": "\n---\n".join(stderr_bits)[-800:],
        }
        if not ok:
            result["error"] = "; ".join(errors) if errors else f"chunked parse produced 0 sections ({pages}p)"
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _is_spec_document(item: object) -> bool:
    category = getattr(item, "category", None)
    if category != FileCategory.SPEC:
        return False
    if getattr(item, "from_split", False):
        return False
    path = getattr(item, "path", None)
    if not isinstance(path, Path):
        return False
    return path.suffix.lower() == ".pdf"


def parse_spec_documents(items: list, estimate_dir: Path | None) -> list[dict]:
    """Write CSI section PDFs for spec documents. Failures stay off ``result.errors``.

    Section files land under ``estimate_dir`` only. They are not returned as
    drawings. When the estimate folder is missing, the whole PDF is left as
    the document ingest already queued.
    """
    results: list[dict] = []
    for item in items:
        if not _is_spec_document(item):
            continue
        pdf: Path = item.path
        try:
            ok, reason = _should_spec_parse(pdf)
        except Exception as exc:
            logger.warning(
                "Spec_Parser gate failed for %s (%s); keeping the whole PDF as a document",
                pdf.name,
                exc,
            )
            results.append({"pdf": str(pdf), "ok": False, "error": str(exc)})
            continue
        if not ok:
            logger.info("Spec_Parser skip %s: %s", pdf.name, reason)
            results.append({"pdf": str(pdf), "ok": False, "skipped": reason})
            continue
        if estimate_dir is None:
            logger.info(
                "Spec_Parser skipped for %s: no existing estimate folder; keeping the whole PDF as a document",
                pdf.name,
            )
            results.append({"pdf": str(pdf), "ok": False, "skipped": "no estimate folder"})
            continue
        dest = spec_split_dir(estimate_dir, pdf)
        tail = dest.relative_to(estimate_dir)
        if "drawings" in {part.lower() for part in tail.parts}:
            logger.warning("Spec_Parser refused to write %s under a drawings path (%s)", pdf.name, dest)
            results.append({"pdf": str(pdf), "ok": False, "error": f"refusing drawings path {dest}"})
            continue
        logger.info("Spec_Parser %s → %s", pdf.name, dest)
        try:
            result = _run_spec_parser(pdf, dest)
        except Exception as exc:
            logger.warning(
                "Spec_Parser failed for %s (%s); keeping the whole PDF as a document",
                pdf.name,
                exc,
            )
            results.append({"pdf": str(pdf), "ok": False, "error": str(exc), "out": str(dest)})
            continue
        results.append(result)
        if result.get("ok"):
            n_out = sum(1 for _ in dest.rglob("*.pdf")) if dest.is_dir() else 0
            logger.info(
                "Spec_Parser wrote %s section PDF(s) for %s under %s (documents, not drawings)",
                n_out,
                pdf.name,
                dest,
            )
        else:
            logger.warning(
                "Spec_Parser failed for %s (%s); keeping the whole PDF as a document",
                pdf.name,
                (result.get("error") or "parse failed").replace("\n", " ")[:300],
            )
    return results
