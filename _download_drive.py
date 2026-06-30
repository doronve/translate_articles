"""Download only the PDF files from the configured Google Drive folder.

All settings (folder URL, output directory, TLS handling, PDFs-only flag)
come from ``config.toml`` via ``_config.py``. Edit the config file instead
of this script.

Uses gdown's internal embedded-folder-view parser to list the folder, then
downloads each PDF individually with ``gdown.download``. Non-PDF entries
(e.g. Google Docs) are skipped when ``[drive].pdfs_only`` is true.
"""

from __future__ import annotations

import sys

from _config import CONFIG, install_tls_bypass

install_tls_bypass()

import gdown  # noqa: E402
from gdown.download import _get_session, _sanitize_filename  # noqa: E402
from gdown.download_folder import _parse_embedded_folder_view  # noqa: E402


def main() -> int:
    output_dir = CONFIG.source_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    folder_id = CONFIG.folder_id
    verify_flag = not CONFIG.disable_tls_verify

    user_agent = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/98.0.4758.102 Safari/537.36"
    )
    sess, _ = _get_session(proxy=None, use_cookies=False, user_agent=user_agent)

    print(f"Listing folder {folder_id}", flush=True)
    _, children = _parse_embedded_folder_view(
        sess=sess, folder_id=folder_id, verify=verify_flag
    )

    targets: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for file_id, file_name, file_type in children:
        if file_type == "application/vnd.google-apps.folder":
            skipped.append((file_id, f"[folder] {file_name}"))
            continue
        if CONFIG.drive_pdfs_only and not file_name.lower().endswith(".pdf"):
            skipped.append((file_id, file_name))
            continue
        targets.append((file_id, _sanitize_filename(filename=file_name)))

    print(
        f"Found {len(targets)} target files; skipping {len(skipped)} entries "
        f"(pdfs_only={CONFIG.drive_pdfs_only})."
    )
    for fid, name in skipped:
        print(f"  skip: {name} ({fid})")

    failures: list[tuple[str, str, str]] = []
    for idx, (file_id, file_name) in enumerate(targets, start=1):
        target = output_dir / file_name
        if target.exists() and target.stat().st_size > 0:
            print(f"[{idx}/{len(targets)}] already exists, skipping: {file_name}")
            continue
        print(f"[{idx}/{len(targets)}] downloading: {file_name}", flush=True)
        try:
            result = gdown.download(
                url=f"https://drive.google.com/uc?id={file_id}",
                output=str(target),
                quiet=False,
                use_cookies=False,
                verify=verify_flag,
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
    print("\nAll target files downloaded successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
