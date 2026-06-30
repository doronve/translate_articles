"""Generate short Hebrew summaries for every processed document.

Reads ``translation_log.json``; for each entry whose step-1 Markdown exists
picks the best available Hebrew source (translated MD if the document was
translated, original MD if the source is already Hebrew) and writes a 2-3
sentence extractive summary back to the log under ``summary``.

The summary is cached: re-running the script is a no-op unless the source
MD has changed (we compare path + size + mtime), unless ``--force`` is
passed. After the log is updated, re-run ``generate_index.py`` to render
the new column.

Usage:
    py -3.13 generate_summaries.py [--force] [--sha <prefix>]

Why extractive (sumy LSA)?
    The pipeline already produces high-quality Hebrew text but we have no
    LLM credentials available. LSA picks the sentences that best span the
    document's main concepts; it is language-agnostic at the math layer
    and works fine on Hebrew once the text is split into sentences. We
    use the default English Punkt tokenizer because Hebrew uses the same
    Latin sentence-terminator punctuation ``.!?``.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from _config import CONFIG, ROOT
from _log import load_log, save_log

SUMMARY_SENTENCES = 3
MIN_SUMMARY_CHARS = 40
MAX_INPUT_CHARS = 60_000

_HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
_PUNCT_END_RE = re.compile(r"(?<=[.!?])\s+(?=[\u0590-\u05FFא-ת\"\'A-Z])")
_URL_RE = re.compile(r"https?://\S+")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")
_CITATION_LINE_RE = re.compile(r"^\s*\(?\d{4}\)?[\s.,;:]*$")
_SOFT_HYPHEN = "\u00ad"

_BOILERPLATE_SUBSTRINGS: tuple[str, ...] = (
    "JSTOR",
    "support@jstor",
    "about.jstor.org",
    "תוכן זה הורד",
    "כל השימוש בכפוף",
    "כל השימוש כפוף",
    "כל הזכויות שמורות",
    "תנאים וההגבלות",
    "אנו משתמשים בטכנולוגיית מידע",
    "downloaded from",
    "all use subject to",
    "All rights reserved",
    "doi.org/",
    "DOI:",
    "ProQuest",
    "ResearchGate",
    "Academia.edu",
    "Cambridge Core",
    "Wiley Online Library",
    "University of Chicago Press",
    "אוניברסיטת שיקגו",
)
_BOILERPLATE_LINES_LOWER = tuple(s.lower() for s in _BOILERPLATE_SUBSTRINGS)


def _hebrew_chars(text: str) -> int:
    return len(_HEBREW_RE.findall(text))


def _strip_markdown(md: str) -> str:
    md = md.replace(_SOFT_HYPHEN, "")
    md = _HTML_COMMENT_RE.sub(" ", md)
    md = _CODE_FENCE_RE.sub(" ", md)
    md = _IMG_RE.sub(" ", md)
    md = _LINK_RE.sub(r"\1", md)
    md = _HTML_TAG_RE.sub(" ", md)
    md = _URL_RE.sub(" ", md)

    cleaned: list[str] = []
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            cleaned.append("")
            continue
        if _TABLE_LINE_RE.match(line):
            continue
        if line.startswith(("|", "---", "===", "***", "___")):
            continue
        line = _HEADING_RE.sub("", line)
        line = re.sub(r"^[\*\-\+>]\s+", "", line)
        line = re.sub(r"^\d+\.\s+", "", line)
        line = re.sub(r"[`*_]+", "", line)
        line = line.strip()
        if _CITATION_LINE_RE.match(line):
            continue
        lower = line.lower()
        if any(b in lower for b in _BOILERPLATE_LINES_LOWER):
            continue
        cleaned.append(line)

    text = "\n".join(cleaned)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_sentences(text: str) -> list[str]:
    """Split into sentences using punctuation, then merge tiny fragments."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    sentences: list[str] = []
    for para in paragraphs:
        flat = re.sub(r"\s+", " ", para)
        parts = _PUNCT_END_RE.split(flat)
        for part in parts:
            part = part.strip()
            if part:
                sentences.append(part)
    return sentences


def _filter_candidate_sentences(sentences: list[str]) -> list[str]:
    """Keep only Hebrew-heavy, prose-like sentences of a useful length."""
    out: list[str] = []
    for s in sentences:
        if len(s) < 40 or len(s) > 600:
            continue
        heb = _hebrew_chars(s)
        if heb < 15 or heb / max(len(s), 1) < 0.25:
            continue
        digits = sum(c.isdigit() for c in s)
        if digits / max(len(s), 1) > 0.35:
            continue
        lower = s.lower()
        if any(b in lower for b in _BOILERPLATE_LINES_LOWER):
            continue
        out.append(s)
    return out


def _sumy_summary(text: str, num_sentences: int) -> list[str]:
    """Run sumy's LSA summarizer; return picked sentences as strings."""
    from sumy.parsers.plaintext import PlaintextParser
    from sumy.nlp.tokenizers import Tokenizer
    from sumy.summarizers.lsa import LsaSummarizer

    parser = PlaintextParser.from_string(text, Tokenizer("english"))
    summarizer = LsaSummarizer()
    picked = summarizer(parser.document, num_sentences)
    return [str(s).strip() for s in picked if str(s).strip()]


def _truncate_for_summarizer(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_break = cut.rfind("\n\n")
    if last_break > max_chars // 2:
        cut = cut[:last_break]
    return cut


def summarize_markdown(md_text: str, num_sentences: int = SUMMARY_SENTENCES) -> str:
    """Produce a short Hebrew summary from a Markdown document."""
    plain = _strip_markdown(md_text)
    if not plain:
        return ""

    candidates = _filter_candidate_sentences(_split_sentences(plain))
    if not candidates:
        return ""
    candidate_text = " ".join(candidates)
    candidate_text = _truncate_for_summarizer(candidate_text, MAX_INPUT_CHARS)

    try:
        picked = _sumy_summary(candidate_text, num_sentences)
    except Exception as exc:  # pragma: no cover - sumy is robust but be safe
        print(f"  sumy failed ({exc!r}); falling back to first sentences",
              file=sys.stderr)
        picked = candidates[:num_sentences]

    seen: set[str] = set()
    ordered: list[str] = []
    for s in picked:
        key = re.sub(r"\s+", " ", s).strip()
        if key and key not in seen:
            seen.add(key)
            ordered.append(s.strip())

    summary = " ".join(ordered).strip()
    if len(summary) < MIN_SUMMARY_CHARS:
        return " ".join(candidates[:num_sentences]).strip()
    return summary


def _pick_source(entry: dict[str, Any]) -> tuple[str, str] | None:
    """Return (relpath, kind) for the best Hebrew MD source, or None."""
    translation = entry.get("translation_md") or {}
    step1 = entry.get("step1") or {}

    if translation.get("status") == "done" and translation.get("translated_md_relpath"):
        return translation["translated_md_relpath"], "translated_md"
    if translation.get("status") == "already_hebrew" and step1.get("md_relpath"):
        return step1["md_relpath"], "step1_md_hebrew"
    return None


def _source_fingerprint(rel: str) -> dict[str, Any] | None:
    abs_path = ROOT / rel
    if not abs_path.exists():
        return None
    stat = abs_path.stat()
    return {"size": stat.st_size, "mtime": int(stat.st_mtime)}


def _summary_is_fresh(
    entry: dict[str, Any],
    rel: str,
    kind: str,
    fingerprint: dict[str, Any],
) -> bool:
    cached = entry.get("summary")
    if not cached:
        return False
    if cached.get("source_relpath") != rel or cached.get("source_kind") != kind:
        return False
    cached_fp = cached.get("source_fingerprint") or {}
    return (
        cached_fp.get("size") == fingerprint["size"]
        and cached_fp.get("mtime") == fingerprint["mtime"]
        and bool(cached.get("text"))
    )


def process_entry(
    entry: dict[str, Any],
    *,
    force: bool,
) -> tuple[str, str]:
    name = entry.get("original_name", entry.get("sha256", "?")[:10])
    pick = _pick_source(entry)
    if pick is None:
        return "skipped", "no Hebrew source available"
    rel, kind = pick

    fingerprint = _source_fingerprint(rel)
    if fingerprint is None:
        return "skipped", f"file missing: {rel}"

    if not force and _summary_is_fresh(entry, rel, kind, fingerprint):
        return "cached", rel

    md_text = (ROOT / rel).read_text(encoding="utf-8")
    summary = summarize_markdown(md_text)
    if not summary:
        return "empty", rel

    entry["summary"] = {
        "text": summary,
        "source_relpath": rel,
        "source_kind": kind,
        "source_fingerprint": fingerprint,
        "method": "sumy-lsa",
        "sentences": SUMMARY_SENTENCES,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return "summarized", rel


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-summarize every document even when a fresh cached summary exists.",
    )
    p.add_argument(
        "--sha",
        default=None,
        help="Only process entries whose sha256 starts with this prefix (debug).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    log = load_log()
    if not log:
        print("Empty log -- nothing to summarize.", file=sys.stderr)
        return 1

    items = sorted(log.items(), key=lambda kv: (kv[1].get("original_name") or "").lower())
    if args.sha:
        items = [(k, v) for k, v in items if k.startswith(args.sha)]
        if not items:
            print(f"No log entry starts with sha={args.sha!r}", file=sys.stderr)
            return 1

    summarized = cached = skipped = empty = 0
    for sha, entry in items:
        name = entry.get("original_name", sha[:10])
        try:
            result, info = process_entry(entry, force=args.force)
        except Exception as exc:  # pragma: no cover - keep going on bad entries
            print(f"  ERROR while summarizing {name!r}: {exc!r}", file=sys.stderr)
            continue

        if result == "summarized":
            summarized += 1
            preview = (entry.get("summary") or {}).get("text", "")
            print(f"  + {name}  ->  {preview[:90]}{'...' if len(preview) > 90 else ''}")
        elif result == "cached":
            cached += 1
            print(f"  = {name}  (cached)")
        elif result == "skipped":
            skipped += 1
            print(f"  - {name}  (skipped: {info})")
        else:
            empty += 1
            print(f"  ? {name}  (no usable sentences: {info})")

    save_log(log)
    print(
        f"\nDone. summarized={summarized} cached={cached} "
        f"skipped={skipped} empty={empty} total={len(items)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
