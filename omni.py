#!/usr/bin/env python3
"""
Omni Flash — Fully standalone video generator.
No external server needed. Runs its own WebSocket + HTTP server
to communicate directly with the Chrome extension.

Usage:
    python omni.py "A dragon breathing fire"
    python omni.py "Eagle soaring" --aspect landscape -o eagle.mp4
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import random
import sys
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Auto-install dependencies
for pkg in ["websockets"]:
    try:
        __import__(pkg)
    except ImportError:
        os.system(f"{sys.executable} -m pip install {pkg} -q")

import websockets

# ─── Config ──────────────────────────────────────────────────

WS_PORT = int(os.environ.get("WS_PORT", "9222"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8100"))
API_KEY = "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"

MODEL_KEY = "abra_t2v_10s"
GENERATE_ENDPOINT = "/v1/video:batchAsyncGenerateVideoText"
POLL_ENDPOINT = "/v1/video:batchCheckAsyncVideoGenerationStatus"
MEDIA_ENDPOINT = "/v1/media/{media_id}"

POLL_INTERVAL = 10
POLL_TIMEOUT = 420
DEFAULT_PROJECT = "0143adf4-5864-4cb4-abb5-fe4254ad0dc7"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("omni")


# ─── Extension Bridge ───────────────────────────────────────

class ExtensionBridge:
    """WebSocket server that Chrome extension connects to."""

    def __init__(self):
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._flow_key = None
        self._connected = asyncio.Event()
        self._loop = None

    async def start(self):
        """Start WS server and HTTP callback server."""
        self._loop = asyncio.get_event_loop()

        # Start HTTP callback server in a thread
        self._start_http_server()

        # Start WS server
        self._ws_server = await websockets.serve(
            self._on_connect, "127.0.0.1", WS_PORT
        )
        log.info("⚡ WebSocket server on ws://127.0.0.1:%d", WS_PORT)
        log.info("⚡ HTTP callback on http://127.0.0.1:%d", HTTP_PORT)
        log.info("⏳ Waiting for Chrome extension to connect...")

    async def wait_for_extension(self, timeout=60):
        """Wait until extension connects and sends flow key."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            log.error("❌ Extension didn't connect in %ds", timeout)
            log.error("   Make sure FlowKit extension is installed and a Flow tab is open")
            return False

    async def _on_connect(self, ws):
        self._ws = ws
        log.info("✅ Extension connected!")
        try:
            async for raw in ws:
                data = json.loads(raw)
                await self._handle_message(data)
        except websockets.exceptions.ConnectionClosed:
            log.warning("Extension disconnected")
            self._ws = None
            self._connected.clear()

    async def _handle_message(self, data):
        msg_type = data.get("type")

        if msg_type == "token_captured":
            self._flow_key = data.get("flowKey")
            log.info("🔑 Auth token captured")
            self._connected.set()

        elif msg_type == "extension_ready":
            log.info("Extension ready (flowKey=%s)", "yes" if data.get("flowKeyPresent") else "no")
            if data.get("flowKeyPresent") and self._flow_key:
                self._connected.set()

        elif msg_type in ("pong", "ping"):
            if msg_type == "ping" and self._ws:
                await self._ws.send(json.dumps({"type": "pong"}))

        else:
            # API response
            req_id = data.get("id")
            if req_id and req_id in self._pending:
                if not self._pending[req_id].done():
                    self._pending[req_id].set_result(data)

    def handle_http_callback(self, data):
        """Called from HTTP thread when extension sends callback."""
        req_id = data.get("id")
        if req_id and req_id in self._pending:
            # Schedule in the asyncio loop
            self._loop.call_soon_threadsafe(
                self._resolve_pending, req_id, data
            )
            return True
        # Handle token_captured via HTTP too
        if data.get("type") == "token_captured":
            self._flow_key = data.get("flowKey")
            self._loop.call_soon_threadsafe(self._connected.set)
            return True
        return False

    def _resolve_pending(self, req_id, data):
        if req_id in self._pending and not self._pending[req_id].done():
            self._pending[req_id].set_result(data)

    async def api_request(self, url_path, body, captcha_action="VIDEO_GENERATION", method="POST"):
        """Send API request through Chrome extension."""
        if not self._ws:
            return {"error": "Extension not connected"}

        req_id = str(uuid.uuid4())
        future = self._loop.create_future()
        self._pending[req_id] = future

        url = f"https://aisandbox-pa.googleapis.com{url_path}?key={API_KEY}"
        ua = random.choice(USER_AGENTS)
        platform = '"macOS"' if "Macintosh" in ua else '"Windows"'

        msg = {
            "id": req_id,
            "method": "api_request",
            "params": {
                "url": url,
                "method": method,
                "headers": {
                    "accept": "*/*",
                    "content-type": "text/plain;charset=UTF-8",
                    "origin": "https://labs.google",
                    "referer": "https://labs.google/",
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": platform,
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "cross-site",
                    "user-agent": ua,
                },
                "body": body,
                "captchaAction": captcha_action,
            },
        }
        await self._ws.send(json.dumps(msg))

        try:
            result = await asyncio.wait_for(future, timeout=90)
            return result
        except asyncio.TimeoutError:
            return {"error": "TIMEOUT"}
        finally:
            self._pending.pop(req_id, None)

    def _start_http_server(self):
        """Start HTTP server for extension callbacks (runs in thread)."""
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path == "/api/ext/callback":
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length)) if length else {}
                    bridge.handle_http_callback(body)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_GET(self):
                if self.path == "/health":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "status": "ok",
                        "extension_connected": bridge._ws is not None,
                    }).encode())
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def log_message(self, *args):
                pass  # Silence HTTP logs

        server = HTTPServer(("127.0.0.1", HTTP_PORT), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    async def close(self):
        self._ws_server.close()
        await self._ws_server.wait_closed()


# ─── Video Generation ───────────────────────────────────────

async def generate_video(bridge, prompt, aspect, project_id):
    """Submit video generation. Returns media_id."""
    body = {
        "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
        "clientContext": {
            "projectId": project_id,
            "tool": "PINHOLE",
            "userPaygateTier": "PAYGATE_TIER_ONE",
            "sessionId": f";{int(time.time() * 1000)}",
            "recaptchaContext": {
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                "token": "",
            },
        },
        "requests": [{
            "aspectRatio": aspect,
            "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
            "videoModelKey": MODEL_KEY,
            "seed": random.randint(1, 9999),
            "metadata": {},
        }],
        "useV2ModelConfig": True,
    }

    log.info('🎬 Generating: "%s" [%s]', prompt[:60], MODEL_KEY)
    result = await bridge.api_request(GENERATE_ENDPOINT, body)

    status = result.get("status", 0)
    if status != 200:
        err = result.get("data", {})
        if isinstance(err, dict):
            err = err.get("error", {}).get("message", result.get("error", "Unknown"))
        log.error("❌ Failed (%s): %s", status, err)
        return None

    data = result.get("data", {})
    media = data.get("media", [])
    if not media:
        log.error("❌ No media in response")
        return None

    media_id = media[0].get("name")
    credits = data.get("remainingCredits", "?")
    log.info("✅ Submitted! media_id=%s, credits=%s", media_id, credits)
    return media_id


async def poll_status(bridge, media_id, project_id):
    """Poll until video ready."""
    body = {"media": [{"name": media_id, "projectId": project_id}]}
    start = time.time()

    while time.time() - start < POLL_TIMEOUT:
        result = await bridge.api_request(POLL_ENDPOINT, body, captcha_action="")
        data = result.get("data", {})
        media = data.get("media", [])

        if media:
            meta = media[0].get("mediaMetadata", {}).get("mediaStatus", {})
            status = meta.get("mediaGenerationStatus", "")

            if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                elapsed = int(time.time() - start)
                log.info("✅ Video ready! (%ds)", elapsed)
                return True
            elif "FAILED" in status or "BLOCKED" in status:
                log.error("❌ Failed: %s", status)
                return False

        elapsed = int(time.time() - start)
        log.info("⏳ Waiting... (%ds)", elapsed)
        await asyncio.sleep(POLL_INTERVAL)

    log.error("❌ Timeout after %ds", POLL_TIMEOUT)
    return False


async def download_video(bridge, media_id, output_path):
    """Download video via get_media API."""
    url_path = MEDIA_ENDPOINT.format(media_id=media_id)
    result = await bridge.api_request(url_path, {}, captcha_action="", method="GET")
    data = result.get("data", result)

    video_b64 = ""
    if isinstance(data, dict):
        v = data.get("video", {})
        if isinstance(v, dict):
            video_b64 = v.get("encodedVideo", "")
        elif isinstance(v, str):
            video_b64 = v

    if not video_b64:
        log.error("❌ No video data in response")
        return False

    video_bytes = base64.b64decode(video_b64)
    with open(output_path, "wb") as f:
        f.write(video_bytes)

    size_mb = len(video_bytes) / (1024 * 1024)
    log.info("✅ Saved: %s (%.1f MB)", output_path, size_mb)
    return True


# ─── Main ────────────────────────────────────────────────────

async def run(args):
    aspect = "VIDEO_ASPECT_RATIO_LANDSCAPE" if args.aspect == "landscape" else "VIDEO_ASPECT_RATIO_PORTRAIT"

    bridge = ExtensionBridge()
    await bridge.start()

    if not await bridge.wait_for_extension(timeout=30):
        return

    media_id = await generate_video(bridge, args.prompt, aspect, args.project_id)
    if not media_id:
        return

    if not await poll_status(bridge, media_id, args.project_id):
        return

    if await download_video(bridge, media_id, args.output):
        log.info("🎉 Done! %s", args.output)
        if sys.platform == "darwin":
            os.system(f'open "{args.output}"')

    await bridge.close()


def main():
    parser = argparse.ArgumentParser(description="Omni Flash — Standalone Video Generator")
    parser.add_argument("prompt", help="Text prompt for video")
    parser.add_argument("--output", "-o", default="omni_output.mp4", help="Output file")
    parser.add_argument("--aspect", "-a", choices=["portrait", "landscape"], default="portrait")
    parser.add_argument("--project-id", "-p", default=DEFAULT_PROJECT)
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
