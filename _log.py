"""Shared, per-PDF status log.

Each entry is keyed by SHA-256 of the source PDF so the same content is
never re-processed (regardless of filename). Every pipeline step adds its
own sub-dict so we keep one row per document but can track each step
independently:

    {
      "<sha256>": {
        "sha256": "...",
        "original_name": "...",
        "duplicates": [...],
        "page_count": 14,
        "has_pictures": true,
        "source_lang": "en",

        "step1": {
          "status": "done" | "error",
          "md_relpath": "...",
          "images_dir_relpath": "...",
          "at": "2026-06-30 09:00:00",
          "error": null
        },

        "translation": {
          "status": "translated" | "already_hebrew" | "error",
          "translated_relpath": "...",
          "original_relpath": "...",
          "at": "...",
          "error": null
        }
      }
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _config import CONFIG, ROOT


def load_log() -> dict[str, dict[str, Any]]:
    if not CONFIG.log_file.exists():
        return {}
    with CONFIG.log_file.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_log(log: dict[str, dict[str, Any]]) -> None:
    CONFIG.log_file.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG.log_file.open("w", encoding="utf-8") as fh:
        json.dump(log, fh, ensure_ascii=False, indent=2, sort_keys=True)


def relpath_from_root(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def ensure_entry(
    log: dict[str, dict[str, Any]],
    sha256: str,
    original_name: str,
) -> dict[str, Any]:
    entry = log.get(sha256)
    if entry is None:
        entry = {
            "sha256": sha256,
            "original_name": original_name,
            "duplicates": [],
            "page_count": 0,
            "has_pictures": False,
            "source_lang": None,
        }
        log[sha256] = entry
    else:
        if entry["original_name"] != original_name and original_name not in entry.get("duplicates", []):
            entry.setdefault("duplicates", []).append(original_name)
    return entry
