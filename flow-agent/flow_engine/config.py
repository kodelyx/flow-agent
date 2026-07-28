"""Flow Agent — Configuration.

All constants hardcoded. No external config files needed.
"""

import os
import shutil
import sys

# ─── Paths ───────────────────────────────────────────────────

if getattr(sys, 'frozen', False):
    ROOT_DIR = os.path.dirname(sys.executable)
else:
    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load user settings from .env. Legacy config.env remains supported so older
# installations continue to work after upgrading.
# Uses setdefault so a real environment variable (shell / launchd) wins over
# the file — matching the MCP server's loader.
def _load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip("'\""))

_load_env_file(os.path.join(ROOT_DIR, ".env"))
_load_env_file(os.path.join(ROOT_DIR, "config.env"))


def _flow_binary_dir() -> str:
    """Locate the directory containing the user-facing ``flow`` executable."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    argv0 = sys.argv[0] if sys.argv else ""
    executable_name = os.path.splitext(os.path.basename(argv0))[0].lower()
    if executable_name == "flow":
        resolved = shutil.which(argv0) if not os.path.dirname(argv0) else argv0
        if resolved:
            return os.path.dirname(os.path.abspath(resolved))
    # Source/module execution: main.py is the development equivalent of the
    # installed binary, so outputs remain beside the checkout instead of cwd.
    return ROOT_DIR


BINARY_DIR = _flow_binary_dir()
_output_override = os.environ.get("FLOW_OUTPUT_DIR") or os.environ.get("OUTPUT_DIR")
OUTPUT_DIR = os.path.abspath(os.path.expanduser(
    _output_override or os.path.join(BINARY_DIR, "output")
))
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Generated history and uploaded media-ID cache share one durable registry.
# ``MEDIA_ID_FILE`` remains an alias for compatibility; it can never point at
# a separate media-ids.json file.
HISTORY_FILE = os.path.abspath(os.path.expanduser(
    os.environ.get("FLOW_HISTORY_FILE", os.path.join(OUTPUT_DIR, "history.json"))
))
MEDIA_ID_FILE = HISTORY_FILE
# This path is read-only and used solely for one-time migration.  New code
# never creates or appends to media-id.js.
LEGACY_MEDIA_ID_FILE = os.path.join(ROOT_DIR, "media-id.js")
LEGACY_MEDIA_STORE_FILE = os.path.join(OUTPUT_DIR, "media-ids.json")

# ─── Project ─────────────────────────────────────────────────

DEFAULT_PROJECT = os.environ.get("DEFAULT_PROJECT", "0143adf4-5864-4cb4-abb5-fe4254ad0dc7")

# Available image models:
# - harbor_seal (Nano Banana 2 Lite)
# - narwhal (Nano Banana)
# - gem_pix_2 (Nano Banana Pro)
IMAGE_MODELS = {
    "harbor_seal": "HARBOR_SEAL",
    "lite": "HARBOR_SEAL",
    "narwhal": "NARWHAL",
    "standard": "NARWHAL",
    "gem_pix_2": "GEM_PIX_2",
    "pro": "GEM_PIX_2",
}

DEFAULT_IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "GEM_PIX_2")
if DEFAULT_IMAGE_MODEL not in IMAGE_MODELS.values():
    # If the env var is one of the keys, resolve it
    DEFAULT_IMAGE_MODEL = IMAGE_MODELS.get(DEFAULT_IMAGE_MODEL.lower(), "GEM_PIX_2")


# ─── Hardcoded constants (never change) ──────────────────────

API_KEY = os.environ.get("API_KEY", "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY")
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
    "generate_fl": "/v1/video:batchAsyncGenerateVideoStartAndEndImage",
    "generate_r2v": "/v1/video:batchAsyncGenerateVideoReferenceImages",
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
    4: 7,
    6: 10,
    8: 12,
    10: 15,
}

# ─── Runtime constants ───────────────────────────────────────

WS_PORT = int(os.environ.get("WS_PORT", "9227"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8100"))

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "420"))

# Max seconds to wait for a single extension roundtrip (a generation submit
# call can legitimately run past a minute, so this must exceed 90s).
API_REQUEST_TIMEOUT = int(os.environ.get("API_REQUEST_TIMEOUT", "180"))

# ─── Rate limiting (protects against Google's UNUSUAL_ACTIVITY throttle) ──────
# Max generation requests allowed to be in flight at the same time.
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "5"))
# Minimum spacing between the start of consecutive generation requests (seconds).
REQUEST_MIN_INTERVAL = float(os.environ.get("REQUEST_MIN_INTERVAL", "3"))

SEGMENT_DURATION = 10
FPS = 24

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
]
