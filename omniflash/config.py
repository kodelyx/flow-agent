"""Omni Flash — Configuration loader.

Loads models.json and exports all config constants.
"""

import json
import os

# ─── Paths ───────────────────────────────────────────────────

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_FILE = os.path.join(ROOT_DIR, "models.json")
MEDIA_ID_FILE = os.path.join(ROOT_DIR, "media-id.js")

# ─── Load config ─────────────────────────────────────────────

with open(MODELS_FILE) as _f:
    _MODELS = json.load(_f)

API_KEY = _MODELS["api_key"]
API_BASE = _MODELS["api_base"]
DEFAULT_PROJECT = _MODELS["default_project"]
CLIENT_CTX = _MODELS["client_context"]
ASPECTS = _MODELS["aspects"]
ENDPOINTS = _MODELS["endpoints"]
DURATIONS = _MODELS["durations"]
DEFAULT_DURATION = max(DURATIONS)

# ─── Constants ───────────────────────────────────────────────

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
