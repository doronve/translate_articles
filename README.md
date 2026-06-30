# translate_articles

Hebrew translations of archaeology articles sourced from a shared Google Drive folder.

## Pipeline (staged)

The project is split into independent steps so each can be inspected /
tuned before moving on:

| # | Script | Input | Output |
|---|---|---|---|
| 0 | `_download_drive.py` | Google Drive folder | `to_translate/*.pdf` |
| 1 | `step1_pdf_to_md.py` | `to_translate/*.pdf` | `step1_md/<basename>/*.md` (+ `images/`) |
| 2 | `translate_md.py` | `step1_md/<basename>/*.md` | `translated_md/<basename>/*.he.md` |
| 3a | `translate_articles.py` | `to_translate/*.pdf` (legacy direct PDF→DOCX flow) | `translated/<basename>/*.he.docx` |
| 3b | `generate_summaries.py` | translated Hebrew MD (or original Hebrew MD) | per-doc `summary.text` cached in `translation_log.json` |
| 3c | `generate_index.py` | `translation_log.json` | `index.html` (browseable table) |

For scanned (image-only) PDFs, step 1 automatically falls back to OCR
via Tesseract (`heb` + `eng` traineddata, shipped in `tessdata_local/`).
The fallback fires when the PDF has fewer than `[step1].ocr_min_chars`
plain-text characters in its text layer; the per-page engine is chosen
by counting characters that land in the script of the expected language.

Every step shares one durable status log (`translation_log.json`) keyed by
SHA-256 of the source PDF; each step writes its own nested object
(`step1`, `translation_md`, `translation`, …) so duplicates are never
re-processed.

## Layout

- `config.toml` — **all tunable parameters** (paths, Drive URL, step 1
  options, translation settings, network options). Edit this file instead
  of changing the scripts.
- `_config.py` — loader for `config.toml` (falls back to built-in defaults if
  the file is missing) and shared TLS-bypass helper for the corporate proxy.
- `_log.py` — shared SHA-256-keyed status log helper.
- `to_translate/` — PDF articles mirrored from the shared Google Drive folder
  [‫מאמרים ותרגומיהם‬](https://drive.google.com/drive/folders/1MxUdFWGuc13JEGEZlnCvtPGMzXXRdCh_).
  Non-PDF entries (Google Docs translations / summaries) are intentionally excluded here.
- `step1_md/<basename>/` — step 1 output: `*.md` in the original language plus
  an `images/` folder for extracted figures.
- `translated_md/<basename>/` — Hebrew Markdown (`*.he.md`) produced from the
  step 1 markdown. Image refs in this file point back to the matching
  `step1_md/<basename>/images/` so figures are never duplicated.
- `translated/<basename>/` — legacy direct-from-PDF flow: original PDF +
  Hebrew DOCX (`*.he.docx`, right-to-left).
- `error_translate/` — PDFs whose translation failed; the error message is
  stored in `translation_log.json`.
- `translation_log.json` — durable record of every file we've seen, keyed by
  SHA-256 so duplicate uploads are never re-processed.
- `index.html` — generated browseable table with columns:
  Original | Short Hebrew summary | Translated MD | Page count | Has pictures |
  Source language | OCR | Per-stage status.
- `_download_drive.py` — pulls the PDFs from Drive into `to_translate/`.
- `step1_pdf_to_md.py` — PDF → English Markdown via `pymupdf4llm`. Optional
  per-paragraph language filter drops e.g. the Slovenian abstract in Gopher.
- `translate_md.py` — English Markdown → Hebrew Markdown (preserves
  headings, lists, image refs, tables, HTML comments).
- `translate_articles.py` — legacy direct PDF → DOCX translation pipeline.
- `generate_summaries.py` — extractive Hebrew summary per document, cached
  back into `translation_log.json` under the `summary` key.
- `generate_index.py` — rebuild `index.html` from the current log.
- `tessdata_local/` — bundled Tesseract language data (`eng`, `osd`,
  `heb`); kept in-repo so OCR works without admin install of language
  packs to `C:\Program Files\Tesseract-OCR\tessdata\`.

## Configuration

All paths and translation settings live in `config.toml`:

| Section | Key | Purpose |
|---|---|---|
| `[paths]` | `source_dir` | where PDFs are read from (and downloaded to) |
| `[paths]` | `translated_dir` | where successful translations land |
| `[paths]` | `error_dir` | where failed PDFs are moved |
| `[paths]` | `log_file` | SHA-256-keyed dedup + status log (JSON) |
| `[paths]` | `index_file` | generated HTML index |
| `[drive]` | `folder_url` | Google Drive folder to mirror |
| `[drive]` | `pdfs_only` | skip non-PDF entries (Docs, sheets, …) |
| `[translation]` | `target_language` | target lang code (`iw` / `he`) |
| `[translation]` | `engine` | translation backend (currently `google`) |
| `[translation]` | `chunk_char_limit` | max chars per request |
| `[translation]` | `retries` | retry attempts per chunk |
| `[translation]` | `retry_backoff_seconds` | initial backoff (× attempt) |
| `[translation]` | `skip_already_hebrew` | skip PDFs already in Hebrew |
| `[docx]` | `font_name`, `font_size_pt` | DOCX output font |
| `[network]` | `disable_tls_verify` | bypass corporate TLS inspection |

Inspect the resolved config any time:

```powershell
py -3.13 _config.py
```

## Setup

Requires Python 3.13 (the version with the dependencies installed on this machine):

```powershell
py -3.13 -m pip install --user gdown pymupdf pymupdf4llm deep-translator python-docx langdetect pytesseract Pillow sumy
```

OCR also needs Tesseract installed on the OS (`winget install --id
UB-Mannheim.TesseractOCR`). The Hebrew/English `*.traineddata` files
live in `tessdata_local/` so no admin install is needed for language
packs; `[step1].tessdata_prefix` in `config.toml` points there.

The helpers force UTF-8 stdio because some filenames are Hebrew:

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

SSL verification is disabled in both helpers (controlled by
`[network].disable_tls_verify` in `config.toml`) because the local network
performs TLS inspection with a self-signed root certificate.

## Refresh PDFs from Google Drive

```powershell
py -3.13 _download_drive.py
```

Re-running is idempotent: files already present in `to_translate/` are skipped.

## Step 1: PDF → English Markdown

```powershell
# all pending PDFs
py -3.13 step1_pdf_to_md.py

# just two test files
py -3.13 step1_pdf_to_md.py --only "Gopher et al. 2001*" --force
py -3.13 step1_pdf_to_md.py --only "Gadot NEA 2019*"     --force
```

Markdown extraction is delegated to `pymupdf4llm`. When `[step1].extract_images`
is true, figures are written to `step1_md/<basename>/images/` and referenced
from the `.md` file. When `[step1].drop_foreign_paragraphs` is true, paragraphs
whose detected language differs from the document's main source language are
dropped (e.g. the Slovenian abstract in Gopher 2001). Re-running is idempotent
unless `--force` is passed.

If the source PDF has no text layer (a scan), step 1 instead renders each
page with PyMuPDF and OCRs it via Tesseract. For each page every configured
language (`[step1].ocr_language` + `[step1].ocr_fallback_languages`) is tried
and the engine that produces the most characters in the *expected script*
wins — e.g. for `heb` we count Hebrew code points, so eng-engine garbage on
a Hebrew page doesn't accidentally win.

## Hebrew summaries (column for the index)

```powershell
# generate / refresh the per-document Hebrew summary cache
py -3.13 generate_summaries.py

# force-regenerate every summary (e.g. after a tweak to the cleaner)
py -3.13 generate_summaries.py --force
```

`generate_summaries.py` picks the best Hebrew source available for each
log entry:

- `translation_md.status == "done"` → use the translated Hebrew MD
  (`translated_md/<basename>/*.he.md`).
- `translation_md.status == "already_hebrew"` → use the original step-1
  Markdown (`step1_md/<basename>/*.md`), which is already in Hebrew.

It strips Markdown formatting and a list of known boilerplate
substrings (JSTOR / "תוכן זה הורד" / copyright lines, etc.), keeps only
Hebrew-heavy candidate sentences, and feeds them through `sumy`'s LSA
extractive summarizer to pick the best ~3 sentences. The result is
stored in `translation_log.json` under `summary.text`. Re-runs are no-ops
unless the source MD has changed (we compare size + mtime) or `--force`
is passed.

## Index page

```powershell
py -3.13 generate_index.py
```

Reads `translation_log.json` and writes `index.html` (open it in a
browser). Columns: original PDF, **short Hebrew summary**, step 1
Markdown, Hebrew Markdown, pages, image count, source language, OCR
flag, per-stage status pill. Re-run any time the log changes (most
importantly: after a new `generate_summaries.py` run).

## Step 2: English MD → Hebrew MD

```powershell
# all entries with step1 done
py -3.13 translate_md.py

# just one file
py -3.13 translate_md.py --only "Gopher et al. 2001*" --force
```

The translator:

- Splits the markdown into blocks (passthrough / heading / list /
  blockquote / prose) and only translates the text content of each.
- Passes images, code fences, tables, HTML comments and horizontal rules
  through verbatim.
- Rewrites image references in the translated MD to point back at
  `../../step1_md/<basename>/images/...` so figures are never duplicated.
- Detects Google Translate error-page responses (occasional "Error 500"
  HTML pages from the free endpoint) and retries with backoff.
- Adds an HTML comment at the top of each Hebrew MD that records the
  source filename, engine, and timestamp.

If you want a Word file later, run pandoc over the produced `.he.md`:

```powershell
pandoc "translated_md/<basename>/<basename>.he.md" -o "<basename>.he.docx" --from gfm
```

## Translate PDFs

```powershell
# everything pending (skips anything already in translation_log.json)
py -3.13 translate_articles.py

# just one file for testing
py -3.13 translate_articles.py --only "Gopher et al. 2001*" --limit 1

# retry files that previously errored
py -3.13 translate_articles.py --reset-errors
```

For each PDF the pipeline:

1. Computes a SHA-256 hash. If that hash is already in `translation_log.json`
   the file is recorded as a duplicate and skipped (no re-translation).
2. Extracts page text and detects whether the PDF has embedded images.
3. Detects the source language.
   - If the PDF is already in Hebrew it is marked `already_hebrew`; only the
     original is copied into `translated/`.
   - Otherwise the text is translated page-by-page in ≤4500-char chunks via
     free Google Translate (`deep_translator`).
4. Writes a Hebrew DOCX (RTL, Arial 11) next to a copy of the original PDF.
5. On any failure the original PDF is moved into `error_translate/` and the
   error is logged.
6. Regenerates `index.html`.

## Caveats

- Free Google Translate is rate-limited; long batches may need backoff or a
  paid backend (Azure Translator / Azure OpenAI). The script already retries
  with exponential backoff up to 3 times per chunk.
- PDF layout (columns, footnotes, figures) is not preserved in the DOCX — the
  output is linear page-by-page text suitable for reading / further editing.
- Images themselves are not embedded in the translated DOCX; the "has
  pictures" column in the index only flags whether the original PDF contained
  raster images.
