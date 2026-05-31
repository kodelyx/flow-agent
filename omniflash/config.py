"""Flow Agent — Configuration.

All constants hardcoded. No external config files needed.
"""

import os

# ─── Paths ───────────────────────────────────────────────────

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_ID_FILE = os.path.join(ROOT_DIR, "media-id.js")

# ─── Project ─────────────────────────────────────────────────

DEFAULT_PROJECT = "0143adf4-5864-4cb4-abb5-fe4254ad0dc7"

# ─── Hardcoded constants (never change) ──────────────────────

API_KEY = "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"
API_BASE = "https://aisandbox-pa.googleapis.com"

CLIENT_CTX = {
    "tool": "PINHOLE",
    "tier": "PAYGATE_TIER_ONE",
    "origin": "https://labs.google",
    "recaptcha_app_type": "RECAPTCHA_APPLICATION_TYPE_WEB",
}

ASPECTS = {
    "portrait": "VIDEO_ASPECT_RATIO_PORTRAIT",
    "landscape": "VIDEO_ASPECT_RATIO_LANDSCAPE",
}

ENDPOINTS = {
    "generate_t2v": "/v1/video:batchAsyncGenerateVideoText",
    "generate_i2v": "/v1/video:batchAsyncGenerateVideoStartImage",
    "generate_edit": "/v1/video:batchAsyncGenerateVideoEditVideo",
    "upload_image": "/v1/flow/uploadImage",
    "poll_status": "/v1/video:batchCheckAsyncVideoGenerationStatus",
    "get_media": "/v1/media/{media_id}",
    "get_credits": "/v1/credits",
}

MODELS = {
    "t2v": {
        4: "abra_t2v_4s",
        6: "abra_t2v_6s",
        8: "abra_t2v_8s",
        10: "abra_t2v_10s",
    },
    "edit": "abra_edit",
}

DURATIONS = [4, 6, 8, 10]
DEFAULT_DURATION = 10
MAX_COUNT = 4

CREDITS_PER_VIDEO = {
    4: 5,
    6: 10,
    8: 10,
    10: 15,
}

# ─── Runtime constants ───────────────────────────────────────

WS_PORT = int(os.environ.get("WS_PORT", "9222"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8100"))

POLL_INTERVAL = 10
POLL_TIMEOUT = 420

SEGMENT_DURATION = 10
FPS = 24

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
]
