#!/usr/bin/env python3
"""Omni Flash — Backward-compatible wrapper.

This file preserves the old `from omni import ...` interface.
All logic lives in the omniflash/ package now.

Usage:
    python omni.py "A dragon breathing fire"
    python omni.py "Eagle soaring" --aspect landscape -o eagle.mp4
"""

# Re-export everything from the package
from omniflash import (
    ExtensionBridge,
    generate_video,
    edit_video,
    upload_image,
    generate_video_i2v,
    poll_status,
    download_video,
    build_client_context,
    upload_video,
    media_store,
    ASPECTS,
    DEFAULT_PROJECT,
    ENDPOINTS,
    CLIENT_CTX,
    API_KEY,
    API_BASE,
)

# CLI entry point
if __name__ == "__main__":
    from cli.generate import main
    main()
