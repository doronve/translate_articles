# translate_articles

Hebrew translations of archaeology articles sourced from a shared Google Drive folder.

## Layout

- `to_translate/` — PDF articles mirrored from the shared Google Drive folder
  [‫מאמרים ותרגומיהם‬](https://drive.google.com/drive/folders/1MxUdFWGuc13JEGEZlnCvtPGMzXXRdCh_).
  Non-PDF entries (Google Docs translations / summaries) are intentionally excluded here.
- `translated/<basename>/` — for each translated PDF, a folder containing both
  the original PDF and a Hebrew DOCX (`*.he.docx`, right-to-left).
- `error_translate/` — PDFs whose translation failed; the error message is
  stored in `translation_log.json`.
- `translation_log.json` — durable record of every file we've seen, keyed by
  SHA-256 so duplicate uploads are never re-translated.
- `index.html` — generated browseable table with columns:
  Original | Translated | Has pictures | Page count | Source language | Translated at.
- `_download_drive.py` — pulls the PDFs from Drive into `to_translate/`.
- `translate_articles.py` — the translation pipeline.

## Setup

Requires Python 3.13 (the version with the dependencies installed on this machine):

```powershell
py -3.13 -m pip install --user gdown pymupdf deep-translator python-docx langdetect
```

The helpers force UTF-8 stdio because some filenames are Hebrew:

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

SSL verification is disabled in both helpers because the local network performs
TLS inspection with a self-signed root certificate.

## Refresh PDFs from Google Drive

```powershell
py -3.13 _download_drive.py
```

Re-running is idempotent: files already present in `to_translate/` are skipped.

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
