"""Generate the browseable HTML index of every PDF the pipeline has seen.

Reads ``translation_log.json`` and produces a single ``index.html`` with a
table of: original PDF, English Markdown (step 1), Hebrew Markdown
(translated), images count, page count, source language, OCR mode flag,
and per-stage status. Run any time after the pipeline updates the log:

    py -3.13 generate_index.py

The script is read-only with respect to the log; it just renders the HTML.
"""
from __future__ import annotations

import html
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from _config import CONFIG, ROOT
from _log import load_log


def _href(rel_path: str | None) -> str | None:
    """URL-encode a workspace-relative path, preserving slashes."""
    if not rel_path:
        return None
    return quote(rel_path.replace("\\", "/"), safe="/")


def _file_link(rel_path: str | None, label: str) -> str:
    """Render a hyperlink to a workspace file, or "—" if missing."""
    if not rel_path:
        return "—"
    abs_path = ROOT / rel_path
    if not abs_path.exists():
        return f'<span class="missing" title="missing on disk">{html.escape(label)}</span>'
    href = _href(rel_path)
    return f'<a href="{html.escape(href or "")}">{html.escape(label)}</a>'


def _yesno(b: bool | None) -> str:
    if b is True:
        return "כן"
    if b is False:
        return "לא"
    return "—"


def _status_pill(step: dict | None, *, success_keys: tuple[str, ...]) -> str:
    """Render a coloured status pill for a per-stage block."""
    if not step:
        return '<span class="pill pending">לא הופעל</span>'
    status = step.get("status") or "—"
    err = step.get("error")
    if status in success_keys:
        return f'<span class="pill ok">{html.escape(status)}</span>'
    if status == "already_hebrew":
        return f'<span class="pill skip">דילוג (עברית)</span>'
    if err or status == "error":
        msg = err or status
        return (
            f'<span class="pill err" title="{html.escape(str(msg))}">'
            f'{html.escape(status)}</span>'
        )
    return f'<span class="pill warn">{html.escape(status)}</span>'


def _summary_cell(entry: dict) -> str:
    """Render the Hebrew summary column with a small attribution footer."""
    summary = entry.get("summary") or {}
    text = (summary.get("text") or "").strip()
    if not text:
        return '<td class="summary-cell"><span class="missing">—</span></td>'

    kind = summary.get("source_kind")
    if kind == "translated_md":
        source_label = "מתוך התרגום לעברית"
    elif kind == "step1_md_hebrew":
        source_label = "מתוך המקור העברי"
    else:
        source_label = kind or ""

    at = summary.get("at") or ""
    title = f"{source_label}{' · ' + at if at else ''}"
    return (
        f'<td class="summary-cell">'
        f'<div class="summary-text" lang="he" dir="rtl">{html.escape(text)}</div>'
        f'<div class="summary-meta" title="{html.escape(title)}">{html.escape(source_label)}</div>'
        f'</td>'
    )


def _row(entry: dict) -> str:
    original_name = entry.get("original_name", "?")
    sha = entry.get("sha256", "")[:8]
    source_dir = CONFIG.source_dir.relative_to(ROOT).as_posix()
    original_rel = f"{source_dir}/{original_name}"

    step1 = entry.get("step1") or {}
    translation = entry.get("translation_md") or {}

    pages = entry.get("page_count")
    image_count = step1.get("image_count")
    has_pictures = entry.get("has_pictures")
    src_lang = entry.get("source_lang") or "—"
    ocr_mode = step1.get("mode") == "ocr"
    text_layer_chars = entry.get("text_layer_chars")

    md_rel = step1.get("md_relpath")
    hebrew_rel = translation.get("translated_md_relpath")

    step1_at = step1.get("at") or "—"
    translation_at = translation.get("at") or "—"

    pictures_cell = _yesno(has_pictures)
    if has_pictures and image_count:
        pictures_cell = f"{_yesno(True)} ({image_count})"

    ocr_cell = (
        f'<span class="pill warn" title="text-layer chars={text_layer_chars}">OCR</span>'
        if ocr_mode
        else "—"
    )

    return f"""<tr>
  <td>{_file_link(original_rel, original_name)}<div class="sha">{html.escape(sha)}</div></td>
  {_summary_cell(entry)}
  <td>{_file_link(md_rel, "Markdown (אנגלית)")}</td>
  <td>{_file_link(hebrew_rel, "Markdown (עברית)")}</td>
  <td class="num">{pages if pages is not None else "—"}</td>
  <td>{pictures_cell}</td>
  <td>{html.escape(src_lang)}</td>
  <td>{ocr_cell}</td>
  <td>{_status_pill(step1, success_keys=("done",))}<div class="when">{html.escape(step1_at)}</div></td>
  <td>{_status_pill(translation, success_keys=("done", "already_hebrew"))}<div class="when">{html.escape(translation_at)}</div></td>
</tr>"""


CSS = """
:root { color-scheme: light; }
body {
  font-family: "Segoe UI", Arial, sans-serif;
  margin: 24px auto;
  max-width: 1640px;
  color: #1f2328;
  background: #fbfbfc;
}
h1 { font-size: 22px; margin: 0 0 6px; }
.subtitle { color: #555; margin: 0 0 16px; font-size: 13px; }
.summary { display: flex; gap: 18px; flex-wrap: wrap; margin: 0 0 20px; }
.summary div {
  background: #fff;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  padding: 8px 14px;
  font-size: 13px;
}
.summary strong { font-size: 18px; display: block; }
table {
  border-collapse: collapse;
  width: 100%;
  background: #fff;
  font-size: 13px;
  table-layout: auto;
}
th, td {
  border: 1px solid #d0d7de;
  padding: 8px 10px;
  text-align: right;
  vertical-align: top;
}
th { background: #f6f8fa; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
.num { text-align: center; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
.sha { color: #999; font-size: 11px; font-family: ui-monospace, Consolas, monospace; }
.when { color: #777; font-size: 11px; margin-top: 4px; }
.missing { color: #999; font-style: italic; }
.summary-cell { width: 360px; max-width: 360px; }
.summary-text {
  font-size: 12.5px;
  line-height: 1.55;
  color: #24292f;
  white-space: normal;
  overflow-wrap: anywhere;
}
.summary-meta {
  color: #8a8f96;
  font-size: 10.5px;
  margin-top: 6px;
  letter-spacing: 0.02em;
}
.pill {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  border: 1px solid transparent;
}
.pill.ok      { background: #dafbe1; color: #0a5a17; border-color: #b5e6c0; }
.pill.skip    { background: #ddf4ff; color: #054168; border-color: #bcd9ed; }
.pill.warn    { background: #fff8c5; color: #6c4e00; border-color: #f0d77b; }
.pill.err     { background: #ffebe9; color: #8b1010; border-color: #ffaba3; }
.pill.pending { background: #eaeef2; color: #57606a; border-color: #d0d7de; }
"""


def render(log: dict) -> str:
    entries = sorted(log.values(), key=lambda e: (e.get("original_name") or "").lower())
    rows = "\n".join(_row(e) for e in entries)

    total = len(entries)
    translated = sum(
        1 for e in entries if (e.get("translation_md") or {}).get("status") == "done"
    )
    already_he = sum(
        1
        for e in entries
        if (e.get("translation_md") or {}).get("status") == "already_hebrew"
    )
    errors = sum(
        1 for e in entries if (e.get("translation_md") or {}).get("status") == "error"
    )
    pending = total - translated - already_he - errors
    ocr_count = sum(
        1 for e in entries if (e.get("step1") or {}).get("mode") == "ocr"
    )
    summary_count = sum(
        1 for e in entries if (e.get("summary") or {}).get("text")
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<title>מאמרים מתורגמים</title>
<style>{CSS}</style>
</head>
<body>
<h1>מאמרים מתורגמים</h1>
<p class="subtitle">נוצר אוטומטית ב-{html.escape(now)} ·
<code dir="ltr">generate_index.py</code></p>

<div class="summary">
  <div><strong>{total}</strong>סה"כ מסמכים</div>
  <div><strong>{translated}</strong>תורגמו לעברית</div>
  <div><strong>{already_he}</strong>כבר בעברית</div>
  <div><strong>{errors}</strong>שגיאות</div>
  <div><strong>{pending}</strong>ממתינים</div>
  <div><strong>{ocr_count}</strong>עברו OCR</div>
  <div><strong>{summary_count}</strong>עם תקציר עברי</div>
</div>

<table>
<thead>
<tr>
  <th>מסמך מקור (PDF)</th>
  <th>תקציר קצר</th>
  <th>שלב 1: Markdown באנגלית</th>
  <th>שלב 2: Markdown בעברית</th>
  <th>עמודים</th>
  <th>תמונות</th>
  <th>שפת מקור</th>
  <th>OCR</th>
  <th>שלב 1 (חילוץ)</th>
  <th>שלב 2 (תרגום)</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""


def main() -> int:
    log = load_log()
    if not log:
        print("Empty log -- nothing to index.", file=sys.stderr)
        return 1
    html_text = render(log)
    out = CONFIG.index_file
    out.write_text(html_text, encoding="utf-8")
    print(f"Wrote {out.relative_to(ROOT)} ({out.stat().st_size:,} bytes; "
          f"{len(log)} entries).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
