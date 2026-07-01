# translate_articles — Usage Guide

Practical, task-oriented documentation. If you want the design
reference (per-file layout, config keys, edge cases) see
[`README.md`](./README.md) instead.

- **Repository:** <https://github.com/doronve/translate_articles>
- **Source PDFs (default):** shared Google Drive folder
  [מאמרים ותרגומיהם](https://drive.google.com/drive/folders/1MxUdFWGuc13JEGEZlnCvtPGMzXXRdCh_)
- **Runtime:** Python 3.13, runs 100% from the command line, no server.

---

## 1. What this project does

Given a folder of PDF articles (English or Hebrew), the pipeline:

1. **Mirrors** the PDFs from Google Drive into `to_translate/`.
2. **Extracts** each PDF to Markdown (`step1_md/`). For scanned PDFs
   with no text layer it automatically falls back to **OCR** (Tesseract,
   Hebrew + English), picking the engine that yields the most
   characters in the expected script.
3. **Translates** English (or any non-Hebrew) content to Hebrew Markdown
   (`translated_md/`), preserving headings, lists, images, tables, HTML
   comments, and citations. Documents already in Hebrew are marked
   `already_hebrew` and left untouched.
4. **Summarises** each document into ~3 Hebrew sentences (extractive
   `sumy` LSA on the translated text, boilerplate lines like JSTOR
   copyright removed first).
5. **Publishes** a browseable `index.html` table linking to every
   original PDF, its English Markdown, its Hebrew Markdown, the short
   Hebrew summary, plus per-stage status pills.

Everything is idempotent and deduplicated by SHA-256 of the source PDF,
so re-runs never re-process a document that has already been handled.
The pipeline log lives in `translation_log.json`.

---

## 2. Where to put source documents

You have three options; use whichever fits your workflow.

### Option A — Google Drive folder (recommended)

Set the folder URL in `config.toml`:

```toml
[drive]
folder_url = "https://drive.google.com/drive/folders/<YOUR_FOLDER_ID>"
pdfs_only  = true
```

Then run `py -3.13 _download_drive.py`. The folder must be shared as
**"Anyone with the link — Viewer"**; no Google account credentials
are needed. Non-PDF entries in the folder are skipped when `pdfs_only`
is true.

### Option B — Drop PDFs into `to_translate/`

Just copy PDFs into `to_translate/` (create it if it doesn't exist) and
skip the download step. The rest of the pipeline reads from
`[paths].source_dir`, which defaults to `to_translate`.

### Option C — Point `source_dir` somewhere else

Edit `config.toml` → `[paths].source_dir` to point at any local folder.
Absolute paths are accepted.

---

## 3. Where the outputs go

All paths are relative to the repository root and configurable in
`config.toml`:

| Folder / file | What it holds |
|---|---|
| `to_translate/` | Original PDFs mirrored from Drive (or dropped manually). |
| `step1_md/<basename>/` | English Markdown + `images/` extracted per PDF. |
| `translated_md/<basename>/` | Hebrew Markdown (`*.he.md`), image refs point back to `step1_md/`. |
| `translated/<basename>/` | Legacy direct PDF→DOCX flow output (original PDF + `*.he.docx`). |
| `error_translate/` | PDFs whose translation failed (moved by the legacy flow). |
| `translation_log.json` | Durable status log, keyed by SHA-256; also holds the per-doc Hebrew `summary`. |
| `index.html` | Auto-generated browseable table. |
| `tessdata_local/` | Bundled Tesseract language data (`eng`, `heb`, `osd`). |

---

## 4. Required access

| Resource | Access needed | Auth |
|---|---|---|
| **Source PDFs on Google Drive** | Public share (`Anyone with the link — Viewer`) on the folder. | None. Handled by `gdown` using the shared URL. |
| **Local disk** | Read/write to the repo folder. | Local user is fine. |
| **Translation (Google free endpoint)** | Outbound HTTPS to `translate.google.com`. | **None**. Free, no API key, but rate-limited and occasionally rejects specific content chunks (the pipeline handles this via a recursive split fallback). |
| **Tesseract OCR** | Local binary + traineddata. Language files are already in `tessdata_local/`. | None (all local). |
| **GitHub (target, optional)** | Only needed if you want to publish the outputs back to a GitHub repo. | HTTPS push token, SSH key, or `gh auth login`. |
| **Corporate proxy with TLS inspection** | Handled by `[network].disable_tls_verify = true` in `config.toml`. | None extra. Turn off on machines with a working CA bundle. |

No cloud credentials (Azure / OpenAI / Google Cloud) are required with
the current default engines. See [§7](#7-ai--translation-engines) if
you want to plug in a paid backend.

---

## 5. First-time setup

```powershell
# 1. Clone the repo
git clone https://github.com/doronve/translate_articles.git
cd translate_articles

# 2. Install Python 3.13 (any distribution) and the dependencies
py -3.13 -m pip install --user gdown pymupdf pymupdf4llm deep-translator `
  python-docx langdetect pytesseract Pillow sumy

# 3. Install Tesseract OCR (Windows via winget; see README for macOS/Linux)
winget install --id UB-Mannheim.TesseractOCR

# 4. UTF-8 stdio so Hebrew filenames render correctly in the console
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# 5. Verify the resolved config
py -3.13 _config.py
```

Windows users hit the 260-char path limit with the deeply-nested
Neolithic images. If a step complains about "Filename too long" run:

```powershell
git config --global core.longpaths true
```

---

## 6. Activating the pipeline (CLI)

Everything runs from the command line. There is no server, no
background daemon, no GUI. Each step is safe to re-run: it processes
only new / changed inputs unless you pass `--force`.

### 6.1 Full pipeline (typical run)

```powershell
py -3.13 _download_drive.py       # 0. mirror Drive → to_translate/
py -3.13 step1_pdf_to_md.py       # 1. PDF → English MD (+ OCR fallback)
py -3.13 translate_md.py          # 2. English MD → Hebrew MD
py -3.13 generate_summaries.py    # 3. Hebrew summary per document
py -3.13 generate_index.py        # 4. Build browseable index.html
```

Open `index.html` in a browser when done.

### 6.2 Iterate on a single document

Every step supports a `--only <glob>` filter (matches on the PDF
basename) and `--force` (ignore the cached status):

```powershell
py -3.13 step1_pdf_to_md.py    --only "Gopher et al. 2001*" --force
py -3.13 translate_md.py       --only "Gopher et al. 2001*" --force
py -3.13 generate_summaries.py --sha 12593
py -3.13 generate_index.py
```

`generate_summaries.py --sha <prefix>` restricts to log entries whose
SHA-256 starts with the given prefix — handy when you tweak the
boilerplate filter and want to re-check one document only.

### 6.3 Refresh only the outputs derived from the log

The summary + index are cheap. After manually editing
`translation_log.json` (e.g. to mark a document as already Hebrew), run:

```powershell
py -3.13 generate_summaries.py
py -3.13 generate_index.py
```

### 6.4 Publish to GitHub (optional)

```powershell
git add step1_md translated_md translation_log.json index.html
git commit -m "Refresh pipeline outputs"
git push origin main
```

The repo does not push automatically; commit and push explicitly when
you want the outputs published.

---

## 7. AI / translation engines

### 7.1 Currently wired (no API keys required)

| Stage | Engine | Notes |
|---|---|---|
| PDF text extraction | `pymupdf4llm` | Deterministic parser; no AI. |
| Scanned-PDF OCR | Tesseract (`eng`, `heb`, `eng+heb`) via `pytesseract` | Language files shipped in `tessdata_local/`. Script-aware scoring picks the best engine per page. |
| Language detection | `langdetect` | Statistical n-gram detector. |
| **Translation** | `deep_translator.GoogleTranslator` (free endpoint) | No API key; rate-limited. On chunk failures the script recursively splits the block down to ~60-char sub-chunks and retries. |
| **Summarisation** | `sumy` LSA extractive | Language-agnostic linear algebra over cleaned sentences. Picks 3 representative Hebrew sentences. No LLM. |

### 7.2 Alternative engines you can plug in

The pipeline is intentionally engine-agnostic. `translate_md.py`
already reads `[translation].engine` from `config.toml` (currently only
`"google"` is implemented), and `generate_summaries.py` has a single
`_sumy_summary()` seam. To swap:

| Stage | Realistic alternatives | Trade-off |
|---|---|---|
| Translation | **DeepL API** (paid, best European quality); **Azure Translator** (paid, per-char); **Azure OpenAI / OpenAI / Anthropic / Gemini** (LLM translation — highest quality but pricier and slower). | Any of these give better Hebrew fluency than Google free, and none have the mid-batch chunk-rejection issue. Would replace `_RetryingTranslator` in `translate_md.py`. |
| OCR | **Azure Document Intelligence**, **Google Document AI**, **AWS Textract**. | Higher accuracy on multi-column academic layouts and mixed-language pages; costs per page. Would replace `ocr_pdf_to_markdown()` in `step1_pdf_to_md.py`. |
| Summarisation | Any LLM (Azure OpenAI, OpenAI, Claude, Gemini). | Gives **abstractive** summaries ("this article argues X") vs. our current **extractive** picks (verbatim sentences). Would replace `_sumy_summary()` in `generate_summaries.py`. |
| Language detection | LLM classification; `fasttext-langdetect`. | Overkill for our needs; `langdetect` is fine. |

If you wire up a paid backend, put its credentials in environment
variables (e.g. `AZURE_OPENAI_API_KEY`, `OPENAI_API_KEY`,
`DEEPL_AUTH_KEY`) and read them from the new engine module. **Do not
commit secrets to the repo.**

---

## 8. Configuration reference

All tunable behaviour lives in `config.toml`. The most commonly
adjusted keys:

```toml
[paths]
source_dir       = "to_translate"     # where PDFs live
translated_md_dir = "translated_md"    # Hebrew MD output

[drive]
folder_url = "https://drive.google.com/drive/folders/..."

[step1]
ocr_when_empty   = true                # OCR scanned PDFs
force_ocr        = false               # OCR every page even if text layer exists
ocr_language     = "eng"               # primary OCR language
ocr_fallback_languages = ["heb", "eng+heb"]
tessdata_prefix  = "tessdata_local"    # project-local traineddata

[translation]
target_language     = "iw"             # "iw" or "he"
engine              = "google"
chunk_char_limit    = 4500
skip_already_hebrew = true

[network]
disable_tls_verify  = true             # bypass corporate TLS inspection
```

Print the fully resolved config any time:

```powershell
py -3.13 _config.py
```

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `tesseract is not installed or it's not in your PATH` | Tesseract missing or not on PATH. | Install via `winget install --id UB-Mannheim.TesseractOCR` and re-open the shell. |
| OCR produces gibberish on a Hebrew scan | Wrong OCR language chosen for that page. | The script already re-runs each page with `[step1].ocr_fallback_languages`; make sure `heb` and `eng+heb` are listed. |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Corporate proxy TLS inspection. | Keep `[network].disable_tls_verify = true` (already the default). |
| `Filename too long` on Windows | 260-char MAX_PATH limit. | `git config --global core.longpaths true`. |
| Translation of one block keeps failing with "api connection error" | Google free endpoint rejecting the specific content. | The script now recursively splits the block down to sentences / halves — just let it run; failing sub-chunks are isolated automatically. |
| `index.html` shows a stale summary | You edited a `.he.md` after generating the summary. | Re-run `py -3.13 generate_summaries.py --force` (or just delete the `summary` block from the log entry). |
| `translated_md_relpath` in the log points nowhere | The `.he.md` was manually removed / re-run failed halfway. | Re-run `py -3.13 translate_md.py --only "<basename>*" --force`. |

---

## 10. TL;DR

```powershell
py -3.13 _download_drive.py
py -3.13 step1_pdf_to_md.py
py -3.13 translate_md.py
py -3.13 generate_summaries.py
py -3.13 generate_index.py
start index.html
```

That's the whole loop. Everything else is caching, retries, and
configuration knobs.
