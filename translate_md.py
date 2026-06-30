"""Translate the English Markdown produced by step 1 into Hebrew Markdown.

Input :  ``<step1_md_dir>/<basename>/<basename>.md``
Output:  ``<translated_md_dir>/<basename>/<basename>.he.md``

Markdown structure (headings, lists, image refs, tables, code fences, HTML
comments, horizontal rules) is preserved verbatim. Only the prose inside
each block is sent to the translation engine, with markdown formatting
markers stripped before translation and re-applied after.

Image references continue to point at ``../../step1_md/.../images/...``
so we don't duplicate the figures on disk. The translated MD is RTL by
nature of being Hebrew; viewers that respect the Unicode bidi algorithm
will render it correctly.

Usage:
    py -3.13 translate_md.py                       # all pending
    py -3.13 translate_md.py --limit 1
    py -3.13 translate_md.py --only "Gopher*"
    py -3.13 translate_md.py --force               # re-translate even if done
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from _config import CONFIG, install_tls_bypass
from _log import load_log, relpath_from_root, save_log

install_tls_bypass()

from deep_translator import GoogleTranslator  # noqa: E402


# ---------------------------------------------------------------------------
# Block parsing
# ---------------------------------------------------------------------------


@dataclass
class Block:
    kind: str  # "passthrough" | "heading" | "list" | "blockquote" | "prose"
    text: str  # original block as it appeared in the file


# Patterns for blocks that must be passed through unchanged.
_PASSTHROUGH_LINE = (
    re.compile(r"^\s*!\[.*\]\(.*\)\s*$"),  # image
    re.compile(r"^\s*<!--.*$"),            # html comment (line containing open)
    re.compile(r".*-->\s*$"),              # html comment close
    re.compile(r"^\s*[`~]{3,}.*$"),        # fenced code marker
    re.compile(r"^\s*\|.*\|\s*$"),         # table row
    re.compile(r"^\s*[-*_]{3,}\s*$"),      # horizontal rule
)

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(?P<indent>\s*)(?P<marker>[-*+]|\d+\.)\s+(?P<text>.+?)\s*$")
_BLOCKQUOTE_RE = re.compile(r"^(?P<prefix>\s*>+\s*)(?P<text>.*)$")


def _is_passthrough_block(block: str) -> bool:
    """Any block whose every non-empty line is a structural markdown line."""
    lines = [ln for ln in block.splitlines() if ln.strip()]
    if not lines:
        return True
    return all(any(p.match(ln) for p in _PASSTHROUGH_LINE) for ln in lines)


def parse_blocks(md_text: str) -> list[Block]:
    """Split the markdown into a list of blocks separated by blank lines."""
    blocks: list[Block] = []
    for raw in re.split(r"(\n\s*\n)", md_text):
        if raw == "":
            continue
        if raw.strip() == "":
            # blank-line separator — keep as passthrough so spacing is preserved
            blocks.append(Block(kind="passthrough", text=raw))
            continue
        if _is_passthrough_block(raw):
            blocks.append(Block(kind="passthrough", text=raw))
            continue
        # Classify by the first non-empty line
        first = next((ln for ln in raw.splitlines() if ln.strip()), "")
        if _HEADING_RE.match(first):
            blocks.append(Block(kind="heading", text=raw))
        elif _LIST_RE.match(first):
            blocks.append(Block(kind="list", text=raw))
        elif _BLOCKQUOTE_RE.match(first):
            blocks.append(Block(kind="blockquote", text=raw))
        else:
            blocks.append(Block(kind="prose", text=raw))
    return blocks


# ---------------------------------------------------------------------------
# Per-line "strip MD -> translate -> wrap MD again" helpers
# ---------------------------------------------------------------------------


def _split_and_translate_inline(text: str, translator: GoogleTranslator) -> str:
    """Translate a chunk of inline text, returning the translation.

    We intentionally do NOT try to strip inline emphasis / link syntax;
    Google Translate handles common punctuation well enough and over-clever
    masking tends to scramble citations.
    """
    text = text.strip()
    if not text:
        return ""
    return translator.translate(text) or ""


def translate_block(block: Block, translator: GoogleTranslator) -> str:
    if block.kind == "passthrough":
        return block.text

    if block.kind == "heading":
        out_lines = []
        for line in block.text.splitlines():
            m = _HEADING_RE.match(line)
            if not m:
                out_lines.append(line)
                continue
            translated = _split_and_translate_inline(m.group("text"), translator)
            out_lines.append(f"{m.group('hashes')} {translated}")
        return "\n".join(out_lines)

    if block.kind == "list":
        out_lines = []
        for line in block.text.splitlines():
            m = _LIST_RE.match(line)
            if not m:
                out_lines.append(line)
                continue
            translated = _split_and_translate_inline(m.group("text"), translator)
            out_lines.append(f"{m.group('indent')}{m.group('marker')} {translated}")
        return "\n".join(out_lines)

    if block.kind == "blockquote":
        out_lines = []
        for line in block.text.splitlines():
            m = _BLOCKQUOTE_RE.match(line)
            if not m:
                out_lines.append(line)
                continue
            translated = _split_and_translate_inline(m.group("text"), translator)
            out_lines.append(f"{m.group('prefix')}{translated}")
        return "\n".join(out_lines)

    # prose — translate the whole block, chunking if needed
    return _translate_long_text(block.text, translator)


def _translate_long_text(text: str, translator: GoogleTranslator) -> str:
    """Translate a prose block, chunking around the configured char limit."""
    limit = CONFIG.chunk_char_limit
    if len(text) <= limit:
        return _split_and_translate_inline(text, translator)
    # split on sentence-ish boundaries
    parts: list[str] = []
    buf = ""
    sentences = re.split(r"(?<=[\.!?])\s+", text)
    for sent in sentences:
        candidate = (buf + " " + sent) if buf else sent
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            parts.append(buf)
        while len(sent) > limit:
            parts.append(sent[:limit])
            sent = sent[limit:]
        buf = sent
    if buf:
        parts.append(buf)
    return " ".join(_split_and_translate_inline(p, translator) for p in parts)


# ---------------------------------------------------------------------------
# Translation engine wrapper with retries
# ---------------------------------------------------------------------------


# Patterns that indicate Google Translate returned an error page instead
# of a real translation. The free endpoint occasionally does this and we
# need to treat it as a transient failure.
_GT_ERROR_SIGNATURES = (
    re.compile(r"Error\s*\d{3}\s*\(", re.IGNORECASE),
    re.compile(r"That\u2019?s an error", re.IGNORECASE),
    re.compile(r"That.{0,3}s all we know", re.IGNORECASE),
    re.compile(r"Please try again later", re.IGNORECASE),
    re.compile(r"<html", re.IGNORECASE),
)


def _looks_like_translate_error_page(text: str) -> bool:
    if not text:
        return False
    head = text[:400]
    return any(p.search(head) for p in _GT_ERROR_SIGNATURES)


class _RetryingTranslator:
    def __init__(self) -> None:
        if CONFIG.engine != "google":
            raise NotImplementedError(
                f"engine '{CONFIG.engine}' is not implemented; "
                "set [translation].engine = \"google\" in config.toml"
            )
        self._engine = GoogleTranslator(source="auto", target=CONFIG.target_language)
        self.retries = max(CONFIG.retries, 5)
        self.backoff = CONFIG.retry_backoff_seconds

    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        last_exc: Exception | None = None
        last_bad: str | None = None
        for attempt in range(self.retries):
            try:
                out = self._engine.translate(text) or ""
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                wait = self.backoff * (attempt + 1)
                print(
                    f"    translate retry {attempt + 1}/{self.retries} "
                    f"after {wait:.1f}s ({exc})",
                    flush=True,
                )
                time.sleep(wait)
                continue
            if _looks_like_translate_error_page(out):
                last_bad = out[:120]
                wait = self.backoff * (attempt + 2)
                print(
                    f"    translate returned error page "
                    f"(attempt {attempt + 1}/{self.retries}); waiting {wait:.1f}s",
                    flush=True,
                )
                time.sleep(wait)
                continue
            return out
        msg = last_bad or str(last_exc) or "unknown failure"
        raise RuntimeError(f"translation failed after {self.retries} attempts: {msg}")


# ---------------------------------------------------------------------------
# Per-file pipeline
# ---------------------------------------------------------------------------


def safe_folder_name(name: str) -> str:
    cleaned = []
    for ch in name:
        cleaned.append("_" if ch in '<>:"/\\|?*' else ch)
    out = "".join(cleaned).strip().strip(".")
    return out or "document"


def _rewrite_image_refs_to_step1(md_text: str, step1_basename: str) -> str:
    """Make image refs point back to step1_md/<basename>/images/...

    The translated MD lives in translated_md/<basename>/, so the
    original ``images/foo.png`` ref would be wrong. Rewrite to a path
    that resolves from translated_md/<basename>/ back to step1_md.
    """
    rel_prefix = f"../../{CONFIG.step1_md_dir.name}/{step1_basename}/"

    def _replace(match: re.Match) -> str:
        target = match.group(2)
        if target.startswith(("http://", "https://", "/", "..", rel_prefix)):
            return match.group(0)
        new_target = rel_prefix + target.lstrip("./")
        return f"![{match.group(1)}]({new_target})"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _replace, md_text)


def translate_one(
    entry: dict,
    translator: _RetryingTranslator,
    force: bool,
) -> dict:
    name = entry["original_name"]
    print(f"\n=== {name} ===", flush=True)

    step1 = entry.get("step1") or {}
    if step1.get("status") != "done" or not step1.get("md_relpath"):
        print("  no step1 output; skipping. run step1_pdf_to_md.py first.", flush=True)
        return entry

    md_path = (Path(step1["md_relpath"])).resolve()
    if not md_path.is_absolute():
        md_path = (Path.cwd() / md_path).resolve()
    if not md_path.exists():
        print(f"  step1 md missing on disk: {md_path}; skipping.", flush=True)
        return entry

    translation_md = entry.get("translation_md") or {}
    if translation_md.get("status") == "done" and not force:
        print(
            f"  translation_md already done -> {translation_md.get('translated_md_relpath')}; "
            "use --force to redo.",
            flush=True,
        )
        return entry

    src_text = md_path.read_text(encoding="utf-8")
    step1_basename = md_path.parent.name
    dest_folder = CONFIG.translated_md_dir / step1_basename
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest_path = dest_folder / (md_path.stem + ".he.md")

    blocks = parse_blocks(src_text)
    prose_block_count = sum(1 for b in blocks if b.kind != "passthrough")
    print(
        f"  blocks: total={len(blocks)} translatable={prose_block_count} "
        f"(chars in source: {len(src_text):,})",
        flush=True,
    )

    try:
        out_parts: list[str] = []
        translated_count = 0
        for idx, block in enumerate(blocks, start=1):
            if block.kind == "passthrough":
                out_parts.append(block.text)
                continue
            translated_count += 1
            print(
                f"    [{translated_count}/{prose_block_count}] block #{idx} "
                f"kind={block.kind} ({len(block.text)} chars)",
                flush=True,
            )
            out_parts.append(translate_block(block, translator))
        out_text = "".join(out_parts)
        out_text = _rewrite_image_refs_to_step1(out_text, step1_basename)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        entry["translation_md"] = {
            "status": "error",
            "translated_md_relpath": None,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(exc),
        }
        print(f"  FAILED: {exc}", flush=True)
        return entry

    # Add a small header so the reader knows what this is.
    header = (
        f"<!-- Translated automatically from {name} via "
        f"deep_translator/{CONFIG.engine} on "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}. Source: "
        f"{step1.get('md_relpath')} -->\n\n"
    )
    dest_path.write_text(header + out_text, encoding="utf-8")

    entry["translation_md"] = {
        "status": "done",
        "translated_md_relpath": relpath_from_root(dest_path),
        "source_md_relpath": step1.get("md_relpath"),
        "engine": CONFIG.engine,
        "target_language": CONFIG.target_language,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error": None,
    }
    print(
        f"  wrote {relpath_from_root(dest_path)} "
        f"({dest_path.stat().st_size:,} bytes)",
        flush=True,
    )
    return entry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", type=str, default=None, help="Glob on original_name")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    CONFIG.translated_md_dir.mkdir(parents=True, exist_ok=True)
    log = load_log()

    candidates = sorted(
        log.values(),
        key=lambda e: e["original_name"].lower(),
    )
    if args.only:
        candidates = [e for e in candidates if fnmatch.fnmatch(e["original_name"], args.only)]
    candidates = [e for e in candidates if (e.get("step1") or {}).get("status") == "done"]

    if not candidates:
        print("nothing to translate. run step1_pdf_to_md.py first.", file=sys.stderr)
        return 1

    translator = _RetryingTranslator()

    processed = 0
    try:
        for entry in candidates:
            tmd = entry.get("translation_md") or {}
            already_done = tmd.get("status") == "done"
            if already_done and not args.force:
                print(
                    f"\n=== {entry['original_name']} ===\n"
                    f"  translation_md already done; skipping.",
                    flush=True,
                )
                continue
            translate_one(entry, translator, args.force)
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
