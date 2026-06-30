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
import urllib.parse
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
    masking tends to scramble citations. On translator failure we fall back
    to splitting (see ``_translate_with_split_fallback``) so a single
    awkward sentence doesn't kill the whole document.
    """
    text = text.strip()
    if not text:
        return ""
    return _translate_with_split_fallback(text, translator)


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


def _split_sentences(text: str) -> list[str]:
    """Split a paragraph into sentence-ish chunks for safe sub-translation."""
    parts = [p for p in re.split(r"(?<=[\.!?])\s+", text) if p.strip()]
    return parts or [text]


def _split_halves(text: str) -> list[str]:
    """Split a string into two halves on the closest whitespace to the middle."""
    if " " not in text:
        return [text]
    mid = len(text) // 2
    left = text.rfind(" ", 0, mid)
    right = text.find(" ", mid)
    cut = left if (left != -1 and (right == -1 or mid - left <= right - mid)) else right
    if cut == -1:
        return [text]
    return [text[:cut].rstrip(), text[cut:].lstrip()]


_MIN_RECURSE_CHARS = 60


def _translate_with_split_fallback(text: str, translator: GoogleTranslator) -> str:
    """Translate ``text``; on failure, recursively split and retry.

    Google's free endpoint occasionally rejects specific 200-400 char
    combinations that individually translate fine. When the retrying
    translator gives up, we fall back to sentence-level splitting, then
    halving, then character-level halving down to ~60 chars. If a sub-chunk
    of <= 60 chars still won't translate, we surface the failure.
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return translator.translate(text) or ""
    except Exception as exc:  # noqa: BLE001
        if len(text) <= _MIN_RECURSE_CHARS:
            raise
        sentences = _split_sentences(text)
        if len(sentences) < 2:
            sentences = _split_halves(text)
            if len(sentences) < 2:
                raise
        print(
            f"    block failed at {len(text)} chars; splitting into "
            f"{len(sentences)} parts and retrying ({exc})",
            flush=True,
        )
        return " ".join(
            _translate_with_split_fallback(p, translator) for p in sentences
        )


def _translate_long_text(text: str, translator: GoogleTranslator) -> str:
    """Translate a prose block, chunking around the configured char limit."""
    limit = CONFIG.chunk_char_limit
    if len(text) <= limit:
        return _translate_with_split_fallback(text, translator)
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
    return " ".join(_translate_with_split_fallback(p, translator) for p in parts)


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

# Substrings in raised exceptions that mean "Google is rate-limiting /
# blocking us"; we need to wait minutes, not seconds, for these.
_RATE_LIMIT_SIGNATURES = (
    "api connection error",
    "Read timed out",
    "Connection aborted",
    "remote end closed connection",
    "ConnectionError",
    "Too Many Requests",
    "RequestError",
)


def _looks_like_translate_error_page(text: str) -> bool:
    if not text:
        return False
    head = text[:400]
    return any(p.search(head) for p in _GT_ERROR_SIGNATURES)


def _looks_like_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(sig.lower() in msg for sig in _RATE_LIMIT_SIGNATURES)


class _RetryingTranslator:
    """Adapter over deep_translator with multi-tier backoff.

    * Transient errors (random HTTP hiccup): short backoff, few retries.
    * Rate-limit / connection errors: long backoff (minutes), more retries
      — the free Google endpoint blocks our IP for ~5-30 minutes after
      too many rapid requests.
    """

    # tier-2 (rate-limit) waits: 30s, 90s.
    # Kept short on purpose: when the failure is actually a single-block
    # content quirk (not a real IP block), we want to bubble up fast so the
    # caller's recursive-split fallback can take over. If the IP is truly
    # blocked, _every_ subsequent block will hit these waits too -- the
    # outer pipeline still gets a chance to back off across many blocks.
    _RATE_LIMIT_WAITS = (30, 90)
    # tier-1 (transient) waits: derived from CONFIG.retry_backoff_seconds

    def __init__(self) -> None:
        if CONFIG.engine != "google":
            raise NotImplementedError(
                f"engine '{CONFIG.engine}' is not implemented; "
                "set [translation].engine = \"google\" in config.toml"
            )
        self._engine = GoogleTranslator(source="auto", target=CONFIG.target_language)
        self.retries = max(CONFIG.retries, 5)
        self.backoff = CONFIG.retry_backoff_seconds
        self._rate_limit_index = 0  # index into _RATE_LIMIT_WAITS for one block

    def _reset_rate_limit(self) -> None:
        self._rate_limit_index = 0

    def _wait_rate_limit(self) -> bool:
        """Sleep using the next rate-limit tier. Return True if we should keep trying."""
        if self._rate_limit_index >= len(self._RATE_LIMIT_WAITS):
            return False
        wait = self._RATE_LIMIT_WAITS[self._rate_limit_index]
        self._rate_limit_index += 1
        print(
            f"    looks like rate-limit / connection issue; "
            f"sleeping {wait}s before retry (tier {self._rate_limit_index}/{len(self._RATE_LIMIT_WAITS)})",
            flush=True,
        )
        time.sleep(wait)
        return True

    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        last_exc: Exception | None = None
        last_bad: str | None = None
        transient_attempts = 0
        while True:
            try:
                out = self._engine.translate(text) or ""
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _looks_like_rate_limit(exc):
                    if not self._wait_rate_limit():
                        break
                    continue
                transient_attempts += 1
                if transient_attempts >= self.retries:
                    break
                wait = self.backoff * transient_attempts
                print(
                    f"    translate retry {transient_attempts}/{self.retries} "
                    f"after {wait:.1f}s ({exc})",
                    flush=True,
                )
                time.sleep(wait)
                continue
            if _looks_like_translate_error_page(out):
                last_bad = out[:120]
                transient_attempts += 1
                if transient_attempts >= self.retries:
                    break
                wait = self.backoff * (transient_attempts + 1)
                print(
                    f"    translate returned error page "
                    f"(attempt {transient_attempts}/{self.retries}); waiting {wait:.1f}s",
                    flush=True,
                )
                time.sleep(wait)
                continue
            # success: reset the rate-limit tier so next call starts fresh
            self._reset_rate_limit()
            return out
        msg = last_bad or str(last_exc) or "unknown failure"
        raise RuntimeError(f"translation failed: {msg}")


# ---------------------------------------------------------------------------
# Per-file pipeline
# ---------------------------------------------------------------------------


def safe_folder_name(name: str) -> str:
    cleaned = []
    for ch in name:
        cleaned.append("_" if ch in '<>:"/\\|?*' else ch)
    out = "".join(cleaned).strip().strip(".")
    return out or "document"


def _encode_md_url(url: str) -> str:
    """URL-encode a markdown image/link target, preserving path structure.

    Spaces and other unsafe characters in image paths break renderers like
    GitHub, VS Code's preview, and most CommonMark viewers — they treat
    everything after the first space as a title attribute. ``quote`` with
    ``safe="/:#?&="`` keeps URL structure intact while encoding spaces
    and other special characters.
    """
    if url.startswith(("http://", "https://", "mailto:", "data:")):
        return url
    return urllib.parse.quote(url, safe="/:#?&=")


def _rewrite_image_refs_to_step1(md_text: str, step1_basename: str) -> str:
    """Make image refs point back to step1_md/<basename>/images/... and
    URL-encode the path so renderers handle spaces correctly.

    The translated MD lives in translated_md/<basename>/, so the
    original ``images/foo.png`` ref would be wrong on its own. We rewrite
    to a path that resolves from translated_md/<basename>/ back to step1_md
    and percent-encode special characters.
    """
    rel_prefix = f"../../{CONFIG.step1_md_dir.name}/{step1_basename}/"

    def _replace(match: re.Match) -> str:
        alt = match.group(1)
        target = match.group(2).strip()
        # Strip optional CommonMark title (e.g. ` "title"`).
        if " " in target and target[0] not in ("<",):
            # Only treat it as having a title if the first token is a valid URL
            # head. To be safe, just use the whole thing as the path.
            pass
        # Already a remote / data URL: leave untouched.
        if target.startswith(("http://", "https://", "mailto:", "data:")):
            return match.group(0)
        # Strip a previous absolute prefix so we don't double-prepend.
        if target.startswith(rel_prefix) or target.startswith(
            urllib.parse.quote(rel_prefix, safe="/:#?&=")
        ):
            # Decode then re-encode to ensure consistent encoding.
            decoded = urllib.parse.unquote(target)
            encoded = _encode_md_url(decoded)
            return f"![{alt}]({encoded})"
        # Treat root-anchored paths as absolute and leave them alone.
        if target.startswith("/"):
            return f"![{alt}]({_encode_md_url(target)})"
        # Anything else (including "../..", "./foo", "images/foo.png") is
        # a relative path from the step 1 .md file. Drop a leading "./",
        # then rewrite onto our rel_prefix.
        cleaned = target[2:] if target.startswith("./") else target
        new_target = rel_prefix + cleaned
        return f"![{alt}]({_encode_md_url(new_target)})"

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
    if translation_md.get("status") in {"done", "already_hebrew"} and not force:
        print(
            f"  translation_md already {translation_md.get('status')} -> "
            f"{translation_md.get('translated_md_relpath') or '(no file)'}; "
            "use --force to redo.",
            flush=True,
        )
        return entry

    src_lang = entry.get("source_lang")
    if src_lang in {"he", "iw"} and CONFIG.skip_already_hebrew:
        entry["translation_md"] = {
            "status": "already_hebrew",
            "translated_md_relpath": None,
            "source_md_relpath": step1.get("md_relpath"),
            "engine": CONFIG.engine,
            "target_language": CONFIG.target_language,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": None,
        }
        print(
            f"  source already in Hebrew (lang={src_lang}); marking as already_hebrew, "
            "no translation produced.",
            flush=True,
        )
        return entry

    src_text = md_path.read_text(encoding="utf-8")
    step1_basename = md_path.parent.name
    dest_folder = CONFIG.translated_md_dir / step1_basename
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest_path = dest_folder / (md_path.stem + ".he.md")
    partial_path = dest_folder / (md_path.stem + ".partial.json")

    blocks = parse_blocks(src_text)
    prose_block_count = sum(1 for b in blocks if b.kind != "passthrough")
    print(
        f"  blocks: total={len(blocks)} translatable={prose_block_count} "
        f"(chars in source: {len(src_text):,})",
        flush=True,
    )

    # Load previous partial (per-block index -> translated text) if present.
    cache: dict[int, str] = {}
    if partial_path.exists():
        try:
            import json as _json
            raw = _json.loads(partial_path.read_text(encoding="utf-8"))
            if raw.get("src_sha") == entry["sha256"]:
                cache = {int(k): v for k, v in (raw.get("blocks") or {}).items()}
                print(f"  resuming from partial: {len(cache)} blocks already translated", flush=True)
            else:
                print("  partial cache present but src changed; ignoring", flush=True)
        except Exception:
            pass

    def _flush_partial() -> None:
        import json as _json
        partial_path.write_text(
            _json.dumps(
                {"src_sha": entry["sha256"], "blocks": {str(k): v for k, v in cache.items()}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    try:
        out_parts: list[str] = []
        translated_count = 0
        for idx, block in enumerate(blocks, start=1):
            if block.kind == "passthrough":
                out_parts.append(block.text)
                continue
            translated_count += 1
            if idx in cache:
                out_parts.append(cache[idx])
                continue
            print(
                f"    [{translated_count}/{prose_block_count}] block #{idx} "
                f"kind={block.kind} ({len(block.text)} chars)",
                flush=True,
            )
            translated = translate_block(block, translator)
            cache[idx] = translated
            out_parts.append(translated)
            if translated_count % 20 == 0:
                _flush_partial()
        out_text = "".join(out_parts)
        out_text = _rewrite_image_refs_to_step1(out_text, step1_basename)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _flush_partial()
        entry["translation_md"] = {
            "status": "error",
            "translated_md_relpath": None,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(exc),
            "partial_relpath": relpath_from_root(partial_path),
            "partial_blocks_done": len(cache),
        }
        print(f"  FAILED: {exc} (partial cache saved with {len(cache)} blocks)", flush=True)
        return entry

    # Add a small header so the reader knows what this is.
    header = (
        f"<!-- Translated automatically from {name} via "
        f"deep_translator/{CONFIG.engine} on "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}. Source: "
        f"{step1.get('md_relpath')} -->\n\n"
    )
    dest_path.write_text(header + out_text, encoding="utf-8")
    # success: clean up partial cache
    if partial_path.exists():
        try:
            partial_path.unlink()
        except Exception:
            pass

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


def fix_image_paths_in_existing(entry: dict) -> bool:
    """Re-apply image-ref rewriting (now URL-encoded) on an existing .he.md.

    Returns True if the file was changed.
    """
    tmd = entry.get("translation_md") or {}
    if tmd.get("status") != "done" or not tmd.get("translated_md_relpath"):
        return False
    md_path = (Path.cwd() / tmd["translated_md_relpath"]).resolve()
    if not md_path.exists():
        print(f"  missing: {md_path}", flush=True)
        return False
    src_md = (Path.cwd() / (tmd.get("source_md_relpath") or "")).resolve()
    step1_basename = src_md.parent.name if src_md.parent.exists() else md_path.parent.name
    before = md_path.read_text(encoding="utf-8")
    after = _rewrite_image_refs_to_step1(before, step1_basename)
    if before == after:
        return False
    md_path.write_text(after, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", type=str, default=None, help="Glob on original_name")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--fix-image-paths-only",
        action="store_true",
        help="Don't translate; just URL-encode image refs in existing translated MD files.",
    )
    args = parser.parse_args()

    CONFIG.translated_md_dir.mkdir(parents=True, exist_ok=True)
    log = load_log()

    candidates = sorted(
        log.values(),
        key=lambda e: e["original_name"].lower(),
    )
    if args.only:
        candidates = [e for e in candidates if fnmatch.fnmatch(e["original_name"], args.only)]

    if args.fix_image_paths_only:
        changed = 0
        for entry in candidates:
            print(f"\n=== {entry['original_name']} ===", flush=True)
            if fix_image_paths_in_existing(entry):
                changed += 1
                print("  image refs rewritten + URL-encoded.", flush=True)
            else:
                print("  no change.", flush=True)
        print(f"\nDone. {changed} file(s) changed.")
        return 0

    candidates = [e for e in candidates if (e.get("step1") or {}).get("status") == "done"]

    if not candidates:
        print("nothing to translate. run step1_pdf_to_md.py first.", file=sys.stderr)
        return 1

    translator = _RetryingTranslator()

    processed = 0
    try:
        for entry in candidates:
            tmd = entry.get("translation_md") or {}
            already_done = tmd.get("status") in {"done", "already_hebrew"}
            if already_done and not args.force:
                print(
                    f"\n=== {entry['original_name']} ===\n"
                    f"  translation_md already {tmd.get('status')}; skipping.",
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
