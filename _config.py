"""Shared configuration loader for the translate_articles project.

Reads ``config.toml`` next to this file. Any missing section/key falls back
to the defaults defined in :data:`DEFAULTS` so the scripts still run if the
config file is absent or partially filled in.

Usage:
    from _config import CONFIG, ROOT
    src_dir = CONFIG.source_dir
"""

from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - we target Python 3.13
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"


DEFAULTS: dict[str, dict[str, Any]] = {
    "paths": {
        "source_dir": "to_translate",
        "translated_dir": "translated",
        "error_dir": "error_translate",
        "log_file": "translation_log.json",
        "index_file": "index.html",
    },
    "drive": {
        "folder_url": "https://drive.google.com/drive/folders/1MxUdFWGuc13JEGEZlnCvtPGMzXXRdCh_",
        "pdfs_only": True,
    },
    "translation": {
        "target_language": "iw",
        "engine": "google",
        "chunk_char_limit": 4500,
        "retries": 3,
        "retry_backoff_seconds": 2.0,
        "skip_already_hebrew": True,
    },
    "docx": {
        "font_name": "Arial",
        "font_size_pt": 11,
    },
    "network": {
        "disable_tls_verify": True,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Shallow-per-section merge: override section values win over base."""
    result: dict[str, Any] = {}
    for section, defaults in base.items():
        if isinstance(defaults, dict):
            merged = dict(defaults)
            user_section = override.get(section, {}) or {}
            if isinstance(user_section, dict):
                merged.update(user_section)
            result[section] = merged
        else:  # pragma: no cover - we only use sectioned config
            result[section] = override.get(section, defaults)
    for section, value in override.items():
        if section not in result:
            result[section] = value
    return result


@dataclass(frozen=True)
class Config:
    """Resolved, typed view of config.toml."""

    # paths
    source_dir: Path
    translated_dir: Path
    error_dir: Path
    log_file: Path
    index_file: Path

    # drive
    drive_folder_url: str
    drive_pdfs_only: bool

    # translation
    target_language: str
    engine: str
    chunk_char_limit: int
    retries: int
    retry_backoff_seconds: float
    skip_already_hebrew: bool

    # docx
    docx_font_name: str
    docx_font_size_pt: int

    # network
    disable_tls_verify: bool

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def folder_id(self) -> str:
        url = self.drive_folder_url.rstrip("/")
        return url.rsplit("/", 1)[-1]


def _resolve_path(value: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    return p


def load_config(path: Path | None = None) -> Config:
    cfg_path = path or CONFIG_PATH
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            user_cfg = tomllib.load(fh)
    else:
        warnings.warn(
            f"config file not found at {cfg_path}; using built-in defaults",
            stacklevel=2,
        )
        user_cfg = {}

    merged = _merge(DEFAULTS, user_cfg)

    paths = merged["paths"]
    drive = merged["drive"]
    tr = merged["translation"]
    docx = merged["docx"]
    network = merged["network"]

    return Config(
        source_dir=_resolve_path(paths["source_dir"]),
        translated_dir=_resolve_path(paths["translated_dir"]),
        error_dir=_resolve_path(paths["error_dir"]),
        log_file=_resolve_path(paths["log_file"]),
        index_file=_resolve_path(paths["index_file"]),
        drive_folder_url=str(drive["folder_url"]),
        drive_pdfs_only=bool(drive["pdfs_only"]),
        target_language=str(tr["target_language"]),
        engine=str(tr["engine"]),
        chunk_char_limit=int(tr["chunk_char_limit"]),
        retries=int(tr["retries"]),
        retry_backoff_seconds=float(tr["retry_backoff_seconds"]),
        skip_already_hebrew=bool(tr["skip_already_hebrew"]),
        docx_font_name=str(docx["font_name"]),
        docx_font_size_pt=int(docx["font_size_pt"]),
        disable_tls_verify=bool(network["disable_tls_verify"]),
        raw=merged,
    )


CONFIG = load_config()


def install_tls_bypass() -> None:
    """Patch ``requests`` to skip TLS verification.

    The corporate proxy on this machine performs TLS inspection with a
    self-signed root that Python's CA bundle does not trust. Both scripts
    call this near the top of execution.
    """
    if not CONFIG.disable_tls_verify:
        return

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


if __name__ == "__main__":
    import json

    print(f"config: {CONFIG_PATH}")
    print(json.dumps(CONFIG.raw, indent=2, ensure_ascii=False, default=str))
    sys.exit(0)
