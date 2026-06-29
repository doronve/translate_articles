"""Download only the PDF files from a public Google Drive folder.

Uses gdown's internal embedded-folder-view parser to list the folder, then
downloads each PDF individually with `gdown.download`. Non-PDF entries
(e.g. Google Docs) are skipped because we only care about source articles.

SSL verification is disabled because the local environment performs TLS
inspection with a self-signed root certificate that Python's CA bundle
does not trust.
"""

from __future__ import annotations

import os
import sys
import warnings

import urllib3
import requests

warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)

_orig_session_request = requests.Session.request


def _no_verify_session_request(self, method, url, **kwargs):
    kwargs["verify"] = False
    return _orig_session_request(self, method, url, **kwargs)


requests.Session.request = _no_verify_session_request


_orig_top_request = requests.api.request


def _no_verify_top_request(method, url, **kwargs):
    kwargs["verify"] = False
    return _orig_top_request(method, url, **kwargs)


requests.api.request = _no_verify_top_request


import gdown  # noqa: E402
from gdown.download import _get_session, _sanitize_filename  # noqa: E402
from gdown.download_folder import _parse_embedded_folder_view  # noqa: E402


FOLDER_ID = "1MxUdFWGuc13JEGEZlnCvtPGMzXXRdCh_"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "to_translate")


def main() -> int:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    user_agent = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/98.0.4758.102 Safari/537.36"
    )
    sess, _ = _get_session(proxy=None, use_cookies=False, user_agent=user_agent)

    print(f"Listing folder {FOLDER_ID}", flush=True)
    _, children = _parse_embedded_folder_view(
        sess=sess, folder_id=FOLDER_ID, verify=False
    )

    pdfs: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for file_id, file_name, file_type in children:
        if file_type == "application/vnd.google-apps.folder":
            skipped.append((file_id, f"[folder] {file_name}"))
            continue
        if not file_name.lower().endswith(".pdf"):
            skipped.append((file_id, file_name))
            continue
        pdfs.append((file_id, _sanitize_filename(filename=file_name)))

    print(f"Found {len(pdfs)} PDF files; skipping {len(skipped)} non-PDF entries.")
    for fid, name in skipped:
        print(f"  skip: {name} ({fid})")

    failures: list[tuple[str, str, str]] = []
    for idx, (file_id, file_name) in enumerate(pdfs, start=1):
        target = os.path.join(OUTPUT_DIR, file_name)
        if os.path.exists(target) and os.path.getsize(target) > 0:
            print(f"[{idx}/{len(pdfs)}] already exists, skipping: {file_name}")
            continue
        print(f"[{idx}/{len(pdfs)}] downloading: {file_name}", flush=True)
        try:
            result = gdown.download(
                url=f"https://drive.google.com/uc?id={file_id}",
                output=target,
                quiet=False,
                use_cookies=False,
                verify=False,
            )
            if not result:
                failures.append((file_id, file_name, "gdown returned None"))
        except Exception as exc:  # noqa: BLE001
            failures.append((file_id, file_name, str(exc)))
            print(f"  FAILED: {exc}", flush=True)

    if failures:
        print("\nFailures:")
        for fid, name, err in failures:
            print(f"  {name} ({fid}): {err}")
        return 1
    print("\nAll PDFs downloaded successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
