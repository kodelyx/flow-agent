#!/usr/bin/env python3
"""Shared runtime state and cross-cutting helpers for the Flow Agent API server.

Holds the ExtensionBridge singleton (read it through ``get_bridge()`` — never
``from flow_server.state import bridge``, which would freeze a stale ``None``), the app
lifespan, the auth dependency, and the publish/history helpers.
"""

import os
import sys
import uuid
import time
import json
import logging
import asyncio
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# Add parent dir to sys.path so flow_engine can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flow_engine import ExtensionBridge
from flow_engine.bridge import target_client_id_var
from flow_engine.generators.t2i import download_image, _parse_image_results

from flow_server.config import OUTPUT_DIR, INPUT_DIR, public_url

# Setup logging (format configured centrally in flow_engine/__init__.py, imported above)
log = logging.getLogger("flow_engine.openai_api")


async def publish(filename: str, out_path: str):
    """Make a generated file web-accessible."""
    return public_url(filename), None


async def recover_orphan_response(data: dict, meta: dict):
    """Handle an extension response whose caller already timed out.

    A generation can succeed on Google Flow but arrive after the request
    timed out. Instead of dropping it, parse any generated media, download
    it, and record it in history so nothing is silently lost.
    """
    try:
        if data.get("status") != 200:
            log.info("Orphan response %s ignored (status=%s)", data.get("id"), data.get("status"))
            return
        # Only images arrive inline; videos are polled separately, so only
        # image generations are recoverable this way.
        if meta.get("captcha_action") != "IMAGE_GENERATION":
            return
        results = _parse_image_results(data.get("data", {}))
        results = [r for r in results if r.get("image_url")]
        if not results:
            return
        prompt = meta.get("prompt", "Recovered generation")
        timestamp = int(time.time())
        recovered = 0
        for i, r in enumerate(results):
            unique_id = uuid.uuid4().hex[:6]
            filename = f"flowagent_img_{timestamp}_{unique_id}_{i+1}.png"
            out_path = os.path.join(OUTPUT_DIR, filename)
            if not await download_image(get_bridge(), r["image_url"], out_path):
                continue
            served_url, r2_key = await publish(filename, out_path)
            await append_to_history("image", served_url, prompt, r.get("media_id"), r2_key)
            recovered += 1
        if recovered:
            log.info("Recovered %d orphaned image(s) from late response %s", recovered, data.get("id"))
    except Exception:
        log.exception("Failed to recover orphan response")

# ExtensionBridge lifecycle
bridge: Optional[ExtensionBridge] = None


def get_bridge():
    return bridge


def set_bridge(b):
    global bridge
    bridge = b


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge
    log.info("Starting Flow Agent Extension Bridge (OpenAI Interface)...")
    bridge = ExtensionBridge()
    bridge.set_orphan_handler(recover_orphan_response)
    await bridge.start()

    # Run extension connection in background
    asyncio.create_task(bridge.wait_for_extension(timeout=30))

    yield

    log.info("Closing Flow Agent Extension Bridge...")
    if bridge:
        await bridge.close()

# Authentication Dependency
security = HTTPBearer(auto_error=False)

async def verify_api_key(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    server_key = os.environ.get("SERVER_API_KEY")
    if not server_key:
        # If no key is defined in .env, auth is skipped (disabled)
        return
    if not credentials or credentials.credentials != server_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Please pass 'Authorization: Bearer <key>'"
        )

# Helper to check bridge health
async def get_active_bridge() -> ExtensionBridge:
    global bridge
    if not bridge:
        raise HTTPException(status_code=503, detail="Extension bridge is not initialized")

    is_healthy = await bridge.health_check()
    if not is_healthy:
        log.info("Bridge health check failed. Re-waiting for extension connection...")
        connected = await bridge.wait_for_extension(timeout=10, max_retries=1)
        if not connected:
            raise HTTPException(
                status_code=503,
                detail="Google Flow extension is not connected. Open Google Flow in Chrome."
            )
    return bridge


# History Management Helper
async def append_to_history(type_str: str, url: str, prompt: str, media_id: str = None, r2_key: str = None):
    """Record a generation. Uses history.json."""
    _append_history_file(type_str, url, prompt, media_id)
    _append_input_file(type_str, prompt, media_id)


def _append_input_file(type_str: str, prompt: str, media_id: str = None):
    """Save the request input alongside its media_id in input/input.json."""
    input_file = os.path.join(INPUT_DIR, "input.json")
    client_id = target_client_id_var.get()
    data = {"inputs": []}
    if os.path.exists(input_file):
        try:
            with open(input_file, "r") as f:
                data = json.load(f)
        except Exception:
            pass
    data["inputs"].insert(0, {
        "type": type_str,
        "prompt": prompt,
        "media_id": media_id,
        "client_id": client_id,
        "timestamp": int(time.time()),
    })
    data["inputs"] = data["inputs"][:100]
    try:
        with open(input_file, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _append_history_file(type_str: str, url: str, prompt: str, media_id: str = None):
    history_file = os.path.join(OUTPUT_DIR, "history.json")
    data = {"history": []}
    if os.path.exists(history_file):
        try:
            with open(history_file, "r") as f:
                data = json.load(f)
        except Exception:
            pass
    data["history"].insert(0, {
        "type": type_str,
        "url": url,
        "prompt": prompt,
        "timestamp": int(time.time()),
        "media_id": media_id
    })
    # Cap history at 100 entries to avoid massive files
    data["history"] = data["history"][:100]
    try:
        with open(history_file, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
