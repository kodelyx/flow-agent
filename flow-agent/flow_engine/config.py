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
    "nano_banana_2": "NARWHAL",
    "standard": "NARWHAL",
    "gem_pix_2": "GEM_PIX_2",
    "pro": "GEM_PIX_2",
}

DEFAULT_IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "NARWHAL")
if DEFAULT_IMAGE_MODEL not in IMAGE_MODELS.values():
    # If the env var is one of the keys, resolve it
    DEFAULT_IMAGE_MODEL = IMAGE_MODELS.get(DEFAULT_IMAGE_MODEL.lower(), "NARWHAL")


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
    "upsample_video": "/v1/video:batchAsyncGenerateVideoUpsampleVideo",
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

# ─── Video upsampling (native generation is 720p) ─────────────
# Flow generates video at 720p and reaches 1080p/4K through a second
# "upsampler" pass on the finished media, exactly like the Flow UI's
# high-resolution download. Model keys and the resolution enum are part of an
# undocumented API, so both stay overridable without a code change.
NATIVE_VIDEO_RESOLUTION = "720p"

VIDEO_UPSAMPLE_MODELS = {
    "1080p": os.environ.get("VIDEO_UPSAMPLER_1080P_MODEL", "veo_3_1_upsampler_1080p"),
    "4k": os.environ.get("VIDEO_UPSAMPLER_4K_MODEL", "veo_3_1_upsampler_4k"),
}

VIDEO_UPSAMPLE_RESOLUTIONS = {
    "1080p": os.environ.get("VIDEO_UPSAMPLE_ENUM_1080P", "VIDEO_RESOLUTION_1080P"),
    "4k": os.environ.get("VIDEO_UPSAMPLE_ENUM_4K", "VIDEO_RESOLUTION_4K"),
}

# 1080p upsampling is free; 4K is a paid, higher-tier operation.
CREDITS_PER_UPSAMPLE = {"1080p": 0, "4k": 50}

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

# Extension transport: auto (HTTP first, WS fallback) | http | ws
EXT_TRANSPORT = os.environ.get("EXT_TRANSPORT", "auto").strip().lower()
EXT_SESSION_TTL_SEC = float(os.environ.get("EXT_SESSION_TTL_SEC", "20"))
EXT_POLL_INTERVAL_MS = int(os.environ.get("EXT_POLL_INTERVAL_MS", "1000"))
ENABLE_EXTENSION_WS = os.environ.get("ENABLE_EXTENSION_WS", "1").strip().lower() in {
    "1", "true", "yes", "on",
}

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "420"))

# Max seconds to wait for a single extension roundtrip (fail fast to allow auto-retry).
API_REQUEST_TIMEOUT = int(os.environ.get("API_REQUEST_TIMEOUT", "60"))

# ─── Rate limiting (protects against Google's UNUSUAL_ACTIVITY throttle) ──────
# Optimal concurrent requests in flight per worker (4 parallel slots).
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "4"))
# Minimum spacing between the start of consecutive generation requests (2.0s stagger).
REQUEST_MIN_INTERVAL = float(os.environ.get("REQUEST_MIN_INTERVAL", "2.0"))

SEGMENT_DURATION = 10
FPS = 24

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
]
