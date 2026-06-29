# translate_articles

Source archaeology articles pending translation.

## Layout

- `to_translate/` — PDF articles mirrored from the shared Google Drive folder
  [‫מאמרים ותרגומיהם‬](https://drive.google.com/drive/folders/1MxUdFWGuc13JEGEZlnCvtPGMzXXRdCh_).
  Non-PDF entries (Google Docs translations / summaries) are intentionally
  excluded here.
- `_download_drive.py` — one-shot helper that lists the Drive folder via
  `gdown` and downloads only the PDFs. Re-running it will skip files that
  already exist locally, so it can be used to refresh the mirror.

## Refreshing the PDFs

Requires Python 3.13 (the version with `gdown` installed on this machine):

```powershell
py -3.13 -m pip install --user gdown
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
py -3.13 _download_drive.py
```

SSL verification is disabled in the helper because the local network performs
TLS inspection with a self-signed root certificate.
