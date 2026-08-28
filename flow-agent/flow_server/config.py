#!/usr/bin/env python3
"""Shared constants and pure helpers for the Flow Agent API server."""

import os
from typing import Optional

from flow_engine.config import OUTPUT_DIR
from flow_server.media_types import extension_for_mime, sniff_media_type

# Directories
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(OUTPUT_DIR, exist_ok=True)
# Backward-compatible name only: inputs and outputs now share one managed
# directory, so the backend never creates an ``input/`` folder beside itself.
INPUT_DIR = OUTPUT_DIR

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "http://localhost:8001"
).rstrip("/")


def public_url(filename: str) -> str:
    return f"{PUBLIC_BASE_URL}/download/{filename}"


def _content_type(filename: str) -> str:
    path = filename if os.path.isabs(filename) else os.path.join(OUTPUT_DIR, filename)
    return sniff_media_type(path, filename=filename)


# Map OpenAI image size to Flow aspect ratio
def map_size_to_aspect(size_str: Optional[str]) -> str:
    if not size_str:
        return "square"

    parts = size_str.lower().split("x")
    if len(parts) == 2:
        try:
            w, h = int(parts[0]), int(parts[1])
            ratio = w / h
            if 0.9 <= ratio <= 1.1:
                return "square"
            elif ratio > 1.4:
                return "landscape"
            elif ratio < 0.7:
                return "portrait"
            elif ratio > 1.0:
                return "4x3"
            else:
                return "3x4"
        except ValueError:
            pass
    return "square"


MCP_SERVER_VERSION = "2.0.7"

DEFAULT_IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gem_pix_2").lower()
_MODEL_ALIASES = {
    "lite": "harbor_seal",
    "harbor_seal": "harbor_seal",
    "standard": "narwhal",
    "narwhal": "narwhal",
    "pro": "gem_pix_2",
    "nano_banana_2": "gem_pix_2",
    "gem_pix_2": "gem_pix_2",
}


def _normalise_model(model):
    key = str(model or DEFAULT_IMAGE_MODEL).strip().lower()
    return _MODEL_ALIASES.get(key, key)


def _safe_filename(name):
    cleaned = os.path.basename(str(name or "")).strip().replace("\x00", "")
    cleaned = "".join(c for c in cleaned if c.isalnum() or c in "._- ").strip()
    return cleaned or "downloaded_media"


def _extension_for_mime(mime):
    return extension_for_mime(mime)
