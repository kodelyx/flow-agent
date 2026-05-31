"""Omni Flash — AI Video Generation & Editing toolkit.

Usage:
    from omniflash import ExtensionBridge, generate_video, poll_status, download_video
    from omniflash import edit_video, upload_image, generate_video_i2v
    from omniflash.config import ASPECTS, DEFAULT_PROJECT
    from omniflash.upload import upload_video
    from omniflash import media_store
"""

# Auto-install dependencies
import os
import sys
for _pkg in ["websockets"]:
    try:
        __import__(_pkg)
    except ImportError:
        os.system(f"{sys.executable} -m pip install {_pkg} -q")

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

# ─── Public API ──────────────────────────────────────────────

from .bridge import ExtensionBridge
from .config import ASPECTS, DEFAULT_PROJECT, ENDPOINTS, CLIENT_CTX, API_KEY, API_BASE
from .generators import (
    generate_video,
    edit_video,
    upload_image,
    generate_video_i2v,
    poll_status,
    download_video,
    build_client_context,
)
from .upload import upload_video
from . import media_store

__all__ = [
    # Bridge
    "ExtensionBridge",
    # Generators
    "generate_video",
    "edit_video",
    "upload_image",
    "generate_video_i2v",
    "poll_status",
    "download_video",
    "build_client_context",
    # Upload
    "upload_video",
    # Config
    "ASPECTS",
    "DEFAULT_PROJECT",
    "ENDPOINTS",
    "CLIENT_CTX",
    "API_KEY",
    "API_BASE",
    # Media store
    "media_store",
]
