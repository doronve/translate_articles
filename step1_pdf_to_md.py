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

import fitz  # PyMuPDF  # noqa: E402
import pymupdf4llm  # noqa: E402
from langdetect import DetectorFactory, detect  # noqa: E402


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


def inspect_pdf(pdf_path: Path) -> tuple[int, bool, str | None]:
    """Return (page_count, has_images, detected_source_lang)."""
    has_images = False
    sample_chunks: list[str] = []
    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
        for page in doc:
            if not has_images and page.get_images(full=False):
                has_images = True
            if sum(len(c) for c in sample_chunks) < 4000:
                sample_chunks.append(page.get_text("text") or "")
    return page_count, has_images, detect_language("\n".join(sample_chunks))


def safe_folder_name(name: str) -> str:
    stem = Path(name).stem
    cleaned = []
    for ch in stem:
        cleaned.append("_" if ch in '<>:"/\\|?*' else ch)
    out = "".join(cleaned).strip().strip(".")
    return out or "document"


def extract_markdown(pdf_path: Path, dest_dir: Path) -> tuple[Path, Path | None, int]:
    """Run pymupdf4llm; return (md_path, images_dir_or_None, image_count).

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
    with chdir(dest_dir):
        md_text = pymupdf4llm.to_markdown(
            str(pdf_abs),
            filename=safe_stem,
            write_images=CONFIG.step1_extract_images,
            image_path=rel_image_dir,
            image_format="png",
            dpi=CONFIG.step1_image_dpi,
        )

    md_name = Path(pdf_path.name).stem + ".md"
    md_path = dest_dir / md_name
    md_path.write_text(md_text, encoding="utf-8")

    image_count = 0
    if images_dir and images_dir.exists():
        image_count = sum(1 for _ in images_dir.iterdir() if _.is_file())

    return md_path, images_dir, image_count


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

    page_count, has_pictures, src_lang = inspect_pdf(pdf_path)
    entry["page_count"] = page_count
    entry["has_pictures"] = has_pictures
    entry["source_lang"] = src_lang
    print(
        f"  pages={page_count} has_pictures={has_pictures} "
        f"detected_lang={src_lang}",
        flush=True,
    )

    dest_dir = CONFIG.step1_md_dir / safe_folder_name(pdf_path.name)
    try:
        md_path, images_dir, image_count = extract_markdown(pdf_path, dest_dir)
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

    entry["step1"] = {
        "status": "done",
        "md_relpath": relpath_from_root(md_path),
        "images_dir_relpath": relpath_from_root(images_dir) if images_dir else None,
        "image_count": image_count,
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
