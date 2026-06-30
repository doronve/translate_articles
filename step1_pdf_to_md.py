"""Step 1 of the pipeline: convert each PDF in ``source_dir`` to Markdown.

Output (still in the *original* language — no translation here):

    <step1_md_dir>/<basename>/<basename>.md
    <step1_md_dir>/<basename>/images/...   (extracted figures, if enabled)

Markdown extraction is delegated to ``pymupdf4llm`` which preserves headings,
lists, tables, and (optionally) embeds images. The full source filename is
used as the folder name so the original/translated/step1 trees stay
visually aligned.

The same SHA-256 dedup as the translator: if a PDF has already been
processed in step 1 it is recorded under the existing log entry as a
duplicate filename and the markdown is *not* regenerated.

Usage:
    py -3.13 step1_pdf_to_md.py                          # all pending
    py -3.13 step1_pdf_to_md.py --limit 2                # first two
    py -3.13 step1_pdf_to_md.py --only "Gopher*"         # glob
    py -3.13 step1_pdf_to_md.py --force                  # re-run even if done
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import os
import sys
import time
import traceback
from pathlib import Path

from _config import CONFIG, install_tls_bypass
from _log import ensure_entry, load_log, relpath_from_root, save_log

install_tls_bypass()

import re  # noqa: E402

import fitz  # PyMuPDF  # noqa: E402
import pymupdf4llm  # noqa: E402
from langdetect import DetectorFactory, detect  # noqa: E402

DetectorFactory.seed = 0


# ---------------------------------------------------------------------------
# OCR helpers (Tesseract)
# ---------------------------------------------------------------------------
_DEFAULT_TESSERACT_DIRS = (
    r"C:\Program Files\Tesseract-OCR",
    r"C:\Program Files (x86)\Tesseract-OCR",
)


def _resolve_tesseract_paths() -> tuple[str | None, str | None]:
    """Return (tesseract_exe, tessdata_dir), preferring config overrides.

    Falls back to the standard UB-Mannheim Windows install location, or
    whatever is already on PATH / TESSDATA_PREFIX. ``tessdata_dir`` is
    always returned as an absolute path because Tesseract resolves it
    relative to its own (temporary) working directory.
    """
    from _config import ROOT as _ROOT

    exe = (CONFIG.step1_tesseract_cmd or "").strip() or None
    tessdata_raw = (CONFIG.step1_tessdata_prefix or "").strip() or None

    if exe is None:
        for d in _DEFAULT_TESSERACT_DIRS:
            cand = Path(d) / "tesseract.exe"
            if cand.exists():
                exe = str(cand)
                break

    tessdata: str | None
    if tessdata_raw:
        candidate = Path(tessdata_raw)
        if not candidate.is_absolute():
            candidate = _ROOT / candidate
        tessdata = str(candidate.resolve())
    else:
        env_td = os.environ.get("TESSDATA_PREFIX")
        if env_td and Path(env_td).exists():
            tessdata = str(Path(env_td).resolve())
        elif exe is not None:
            cand = Path(exe).parent / "tessdata"
            tessdata = str(cand.resolve()) if cand.exists() else None
        else:
            tessdata = None

    return exe, tessdata


def _ensure_tesseract_on_path() -> tuple[str | None, str | None]:
    """Make sure pymupdf can find tesseract; export env vars in-process."""
    exe, tessdata = _resolve_tesseract_paths()
    if exe:
        bin_dir = str(Path(exe).parent)
        existing = os.environ.get("PATH", "")
        if bin_dir not in existing.split(os.pathsep):
            os.environ["PATH"] = bin_dir + os.pathsep + existing
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata
    return exe, tessdata


def _strip_text_for_count(s: str) -> str:
    """Letters-and-digits only count for OCR-trigger threshold."""
    return re.sub(r"[^\w\u0590-\u05FF\u0600-\u06FF\u4e00-\u9fff]", "", s or "")


_LATIN_RE = re.compile(r"[A-Za-z]")
_HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")

# Map Tesseract language codes to (langdetect ISO 639-1, script regex)
_LANG_INFO = {
    "eng": ("en", _LATIN_RE),
    "fra": ("fr", _LATIN_RE),
    "deu": ("de", _LATIN_RE),
    "spa": ("es", _LATIN_RE),
    "ita": ("it", _LATIN_RE),
    "lat": ("la", _LATIN_RE),
    "heb": ("he", _HEBREW_RE),
    "yid": ("yi", _HEBREW_RE),
    "ara": ("ar", _ARABIC_RE),
}


def _expected_chars(text: str, lang: str) -> int:
    """Count characters in the expected script(s) for ``lang``.

    For multi-language like "eng+heb", sums chars in BOTH scripts.
    """
    total = 0
    for code in lang.split("+"):
        info = _LANG_INFO.get(code.strip().lower())
        if info:
            total += len(info[1].findall(text or ""))
    return total


def _score_ocr_text(text: str, lang: str) -> tuple[int, int]:
    """Return (script_chars, total_chars) -- higher script_chars = better OCR.

    For a page whose true content is Hebrew, the heb engine outputs
    Hebrew code points while the eng engine outputs Latin gibberish that
    contains zero Hebrew characters. So counting characters in the
    *expected* script per engine reliably identifies the correct one.
    """
    script_chars = _expected_chars(text, lang)
    return script_chars, len(text or "")


def _render_page_image(page, dpi: int):
    """Return a PIL Image rendered from a PyMuPDF page at the given DPI."""
    from PIL import Image
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def ocr_pdf_to_markdown(
    pdf_path: Path,
    language: str,
    dpi: int,
    fallback_languages: tuple[str, ...] = (),
) -> str:
    """OCR every page with Tesseract; auto-retry per page with fallback langs.

    For each page we render it once, then call Tesseract for the primary
    language and (if quality is poor) each fallback language. The best
    scoring result wins. Pages are separated by an HTML comment marker so
    downstream tools (and humans) can see the original page boundaries.
    """
    import pytesseract
    from PIL import Image  # noqa: F401  -- import to fail early

    exe, tessdata = _ensure_tesseract_on_path()
    if exe is None:
        raise RuntimeError(
            "Tesseract executable not found. Install UB-Mannheim Tesseract "
            "or set [step1].tesseract_cmd in config.toml."
        )
    pytesseract.pytesseract.tesseract_cmd = exe
    print(
        f"  OCR: tesseract={exe} primary={language} "
        f"fallbacks={list(fallback_languages)} dpi={dpi} "
        f"tessdata={tessdata or '(env)'}",
        flush=True,
    )
    base_cfg = "--oem 3 --psm 3"
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata

    languages = [language, *fallback_languages]
    parts: list[str] = []
    lang_usage: dict[str, int] = {}

    with fitz.open(pdf_path) as doc:
        n = doc.page_count
        for i, page in enumerate(doc, start=1):
            img = _render_page_image(page, dpi)
            best_text = ""
            best_script_chars = -1
            best_lang = language
            tried: list[str] = []
            for lang in languages:
                try:
                    txt = pytesseract.image_to_string(
                        img, lang=lang, config=base_cfg
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"    page {i}/{n} [{lang}]: error {exc}", flush=True)
                    tried.append(f"{lang}=error")
                    continue
                script_chars, total_chars = _score_ocr_text(txt, lang)
                tried.append(f"{lang}={script_chars}/{total_chars}")
                if script_chars > best_script_chars:
                    best_script_chars = script_chars
                    best_text = txt
                    best_lang = lang

            best_text = best_text.strip()
            if best_text:
                parts.append(
                    f"<!-- page {i} (lang={best_lang}) -->\n\n{best_text}"
                )
                print(
                    f"    page {i}/{n}: picked {best_lang} "
                    f"(script_chars={best_script_chars}); tried {tried}",
                    flush=True,
                )
                lang_usage[best_lang] = lang_usage.get(best_lang, 0) + 1
            else:
                parts.append(f"<!-- page {i} (no text) -->")
                print(f"    page {i}/{n}: no text", flush=True)

    if lang_usage:
        winners = ", ".join(f"{k}={v}p" for k, v in sorted(lang_usage.items()))
        print(f"  OCR done; language usage by page: {winners}", flush=True)
    return "\n\n".join(parts).strip() + "\n"


# Markdown blocks we never want to language-classify (and should always keep).
_MD_KEEP_PATTERNS = (
    re.compile(r"^\s*!\[.*\]\(.*\)\s*$"),         # image
    re.compile(r"^\s*<!--.*$"),                   # html comment (open / inline)
    re.compile(r".*-->\s*$"),                     # html comment (close)
    re.compile(r"^\s*[`~]{3,}.*$"),               # fenced code start/end
    re.compile(r"^\s*\|.*\|\s*$"),                # table row
    re.compile(r"^\s*[-*_]{3,}\s*$"),             # horizontal rule
)
_LETTERY_RE = re.compile(
    r"[A-Za-z\u0370-\u03FF\u0400-\u04FF\u0530-\u058F\u0590-\u05FF\u0600-\u06FF]"
)


@contextlib.contextmanager
def chdir(target: Path):
    prev = Path.cwd()
    os.chdir(target)
    try:
        yield
    finally:
        os.chdir(prev)

DetectorFactory.seed = 0


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def detect_language(sample_text: str) -> str | None:
    sample = (sample_text or "").strip()
    if len(sample) < 40:
        return None
    try:
        return detect(sample[:4000])
    except Exception:
        return None


def _block_is_keep_pattern(block: str) -> bool:
    """True for markdown blocks that should never be lang-filtered."""
    for line in block.splitlines():
        if any(p.match(line) for p in _MD_KEEP_PATTERNS):
            return True
    return False


def _strip_md_for_lang_detect(block: str) -> str:
    """Strip headings, emphasis, lists, etc. before lang detection."""
    text = block
    # remove inline emphasis markers
    text = re.sub(r"[*_`~]+", " ", text)
    # remove leading list / heading markers
    text = re.sub(r"(?m)^\s*(#{1,6}|[-*+]|\d+\.)\s+", "", text)
    # collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def filter_foreign_paragraphs(md_text: str, doc_lang: str | None) -> tuple[str, int]:
    """Drop paragraphs whose detected language differs from doc_lang.

    Short paragraphs (<= 80 chars of letters), markdown structural blocks
    (images, code fences, tables, hrules, comments), and paragraphs we
    can't classify are always kept. Returns (filtered_text, dropped_count).
    """
    if not doc_lang:
        return md_text, 0

    blocks = re.split(r"\n\s*\n", md_text)
    kept: list[str] = []
    dropped = 0
    for block in blocks:
        if not block.strip():
            kept.append(block)
            continue
        if _block_is_keep_pattern(block):
            kept.append(block)
            continue
        stripped = _strip_md_for_lang_detect(block)
        letter_count = len(_LETTERY_RE.findall(stripped))
        if letter_count <= 80:
            kept.append(block)
            continue
        try:
            block_lang = detect(stripped[:4000])
        except Exception:
            kept.append(block)
            continue
        if block_lang == doc_lang:
            kept.append(block)
        else:
            dropped += 1
    return "\n\n".join(kept), dropped


def inspect_pdf(pdf_path: Path) -> tuple[int, bool, str | None, int]:
    """Return (page_count, has_images, detected_source_lang, text_layer_chars).

    ``text_layer_chars`` counts the *total* characters present in the PDF
    text layer across all pages -- a strong signal for whether the doc is
    scanned (≈0) or already searchable.
    """
    has_images = False
    sample_chunks: list[str] = []
    total_chars = 0
    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
        for page in doc:
            if not has_images and page.get_images(full=False):
                has_images = True
            page_text = page.get_text("text") or ""
            total_chars += len(page_text)
            if sum(len(c) for c in sample_chunks) < 4000:
                sample_chunks.append(page_text)
    return (
        page_count,
        has_images,
        detect_language("\n".join(sample_chunks)),
        total_chars,
    )


def safe_folder_name(name: str) -> str:
    stem = Path(name).stem
    cleaned = []
    for ch in stem:
        cleaned.append("_" if ch in '<>:"/\\|?*' else ch)
    out = "".join(cleaned).strip().strip(".")
    return out or "document"


def extract_markdown(
    pdf_path: Path, dest_dir: Path, text_layer_chars: int
) -> tuple[Path, Path | None, int, str]:
    """Run pymupdf4llm or OCR; return (md_path, images_dir, image_count, mode).

    ``mode`` is one of ``"text"`` (normal text-layer extraction) or ``"ocr"``
    (fell back to Tesseract because the PDF was scanned). The decision is
    made *before* invoking pymupdf4llm so we avoid having it mangle OCR'd
    text into noisy "table" cells.

    Works around a pymupdf4llm quirk where it returns the same string for
    both the markdown image reference and the on-disk save path, after
    replacing spaces with underscores. We do that by chdir'ing into
    ``dest_dir`` and passing a clean relative ``image_path`` and
    ``filename`` so the produced strings always resolve.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    images_dir: Path | None = None
    if CONFIG.step1_extract_images:
        images_dir = dest_dir / CONFIG.step1_images_subdir
        images_dir.mkdir(parents=True, exist_ok=True)

    safe_stem = safe_folder_name(pdf_path.name)
    rel_image_dir = CONFIG.step1_images_subdir if images_dir else ""

    pdf_abs = pdf_path.resolve()

    use_ocr = CONFIG.step1_force_ocr or (
        CONFIG.step1_ocr_when_empty
        and text_layer_chars < CONFIG.step1_ocr_min_chars
    )

    if use_ocr:
        if CONFIG.step1_force_ocr:
            print("  force_ocr=true; running OCR", flush=True)
        else:
            print(
                f"  text layer is empty ({text_layer_chars} chars < "
                f"{CONFIG.step1_ocr_min_chars}); using OCR directly",
                flush=True,
            )
        md_text = ocr_pdf_to_markdown(
            pdf_path,
            language=CONFIG.step1_ocr_language,
            dpi=CONFIG.step1_ocr_dpi,
            fallback_languages=CONFIG.step1_ocr_fallback_languages,
        )
        mode = "ocr"
    else:
        with chdir(dest_dir):
            md_text = pymupdf4llm.to_markdown(
                str(pdf_abs),
                filename=safe_stem,
                write_images=CONFIG.step1_extract_images,
                image_path=rel_image_dir,
                image_format="png",
                dpi=CONFIG.step1_image_dpi,
            )
        mode = "text"

    md_name = Path(pdf_path.name).stem + ".md"
    md_path = dest_dir / md_name
    md_path.write_text(md_text, encoding="utf-8")

    image_count = 0
    if images_dir and images_dir.exists():
        image_count = sum(1 for _ in images_dir.iterdir() if _.is_file())

    return md_path, images_dir, image_count, mode


def process_pdf(pdf_path: Path, log: dict, force: bool) -> dict:
    print(f"\n=== {pdf_path.name} ===", flush=True)
    digest = sha256_of(pdf_path)
    entry = ensure_entry(log, digest, pdf_path.name)

    step1 = entry.get("step1")
    if step1 and step1.get("status") == "done" and not force:
        print(
            f"  step1 already done -> {step1.get('md_relpath')}; "
            "use --force to regenerate.",
            flush=True,
        )
        return entry

    page_count, has_pictures, src_lang, text_layer_chars = inspect_pdf(pdf_path)
    entry["page_count"] = page_count
    entry["has_pictures"] = has_pictures
    entry["source_lang"] = src_lang
    entry["text_layer_chars"] = text_layer_chars
    print(
        f"  pages={page_count} has_pictures={has_pictures} "
        f"text_layer_chars={text_layer_chars} detected_lang={src_lang}",
        flush=True,
    )

    dest_dir = CONFIG.step1_md_dir / safe_folder_name(pdf_path.name)
    try:
        md_path, images_dir, image_count, mode = extract_markdown(
            pdf_path, dest_dir, text_layer_chars
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        entry["step1"] = {
            "status": "error",
            "md_relpath": None,
            "images_dir_relpath": None,
            "image_count": 0,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(exc),
        }
        print(f"  FAILED: {exc}", flush=True)
        return entry

    dropped = 0
    if CONFIG.step1_drop_foreign_paragraphs and mode == "text":
        original_text = md_path.read_text(encoding="utf-8")
        filtered_text, dropped = filter_foreign_paragraphs(original_text, src_lang)
        if dropped:
            md_path.write_text(filtered_text, encoding="utf-8")
            print(f"  dropped {dropped} foreign-language paragraph(s)", flush=True)

    if mode == "ocr":
        ocr_text = md_path.read_text(encoding="utf-8")
        re_lang = detect_language(re.sub(r"<!--.*?-->", "", ocr_text, flags=re.S))
        if re_lang:
            entry["source_lang"] = re_lang

    entry["step1"] = {
        "status": "done",
        "md_relpath": relpath_from_root(md_path),
        "images_dir_relpath": relpath_from_root(images_dir) if images_dir else None,
        "image_count": image_count,
        "dropped_foreign_paragraphs": dropped,
        "mode": mode,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error": None,
    }
    print(
        f"  wrote {relpath_from_root(md_path)} "
        f"({md_path.stat().st_size:,} bytes, {image_count} images)",
        flush=True,
    )
    return entry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Process at most N pending PDFs")
    parser.add_argument("--only", type=str, default=None, help="Glob pattern; only matching filenames are processed")
    parser.add_argument("--force", action="store_true", help="Regenerate markdown even if step1 is already done")
    args = parser.parse_args()

    if not CONFIG.source_dir.exists():
        print(f"Source dir not found: {CONFIG.source_dir}", file=sys.stderr)
        return 2

    CONFIG.step1_md_dir.mkdir(parents=True, exist_ok=True)

    log = load_log()

    pdfs = sorted(
        p for p in CONFIG.source_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
    )
    if args.only:
        pdfs = [p for p in pdfs if fnmatch.fnmatch(p.name, args.only)]

    processed = 0
    try:
        for pdf in pdfs:
            digest = sha256_of(pdf)
            existing = log.get(digest)
            already_done = (
                existing is not None
                and existing.get("step1", {}).get("status") == "done"
            )
            if already_done and not args.force:
                process_pdf(pdf, log, args.force)  # no-op, only updates duplicates
                continue
            process_pdf(pdf, log, args.force)
            processed += 1
            if args.limit is not None and processed >= args.limit:
                print(f"\nHit --limit={args.limit}; stopping.", flush=True)
                break
    finally:
        save_log(log)
        print(f"\nLog: {relpath_from_root(CONFIG.log_file)}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
