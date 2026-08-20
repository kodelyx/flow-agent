"""Flow Engine — AI Video Generation & Editing toolkit.

Usage:
    from flow_engine import ExtensionBridge, generate_video, poll_status, download_video
    from flow_engine import edit_video, upload_image, generate_video_i2v
    from flow_engine.config import ASPECTS, DEFAULT_PROJECT
    from flow_engine.upload import upload_video
    from flow_engine import media_store
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet third-party access-log noise; our own logs are enough.
for _noisy in ("uvicorn.access", "websockets", "websockets.server"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ─── Public API ──────────────────────────────────────────────

from .bridge import ExtensionBridge
from .config import (
    ASPECTS, DEFAULT_PROJECT, ENDPOINTS, CLIENT_CTX, API_KEY, API_BASE,
    NATIVE_VIDEO_RESOLUTION, VIDEO_UPSAMPLE_MODELS, CREDITS_PER_UPSAMPLE,
)
from .generators import (
    generate_video,
    edit_video,
    upload_image,
    generate_video_i2v,
    upsample_video,
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
    "upsample_video",
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
    "NATIVE_VIDEO_RESOLUTION",
    "VIDEO_UPSAMPLE_MODELS",
    "CREDITS_PER_UPSAMPLE",
    # Media store
    "media_store",
]
