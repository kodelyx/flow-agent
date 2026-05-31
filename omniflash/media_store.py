"""Omni Flash — Media ID store.

Reads and writes media-id.js (filename → media_id mapping).
Single source of truth — used by upload, upload_image, etc.
"""

import logging
import os

from .config import MEDIA_ID_FILE

log = logging.getLogger("omniflash.media_store")


def read_entries() -> dict[str, str]:
    """Read all filename → media_id entries from media-id.js."""
    entries = {}
    if os.path.exists(MEDIA_ID_FILE):
        with open(MEDIA_ID_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if " : " in line:
                    k, v = line.split(" : ", 1)
                    entries[k.strip()] = v.strip()
    return entries


def write_entries(entries: dict[str, str]):
    """Write all entries to media-id.js (sorted)."""
    with open(MEDIA_ID_FILE, "w") as f:
        for k, v in sorted(entries.items()):
            f.write(f"{k} : {v}\n")


def save(filename: str, media_id: str):
    """Add or update a single entry in media-id.js."""
    entries = read_entries()
    entries[filename] = media_id
    write_entries(entries)
    log.info("📝 Updated media-id.js: %s → %s", filename, media_id)


def get(filename: str) -> str | None:
    """Get media_id for a filename, or None."""
    return read_entries().get(filename)
