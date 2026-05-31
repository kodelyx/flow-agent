"""Omni Flash — ExtensionBridge.

WebSocket + HTTP server that communicates with the Chrome extension.
Handles auth token capture, API proxying, and request/response routing.
"""

import asyncio
import json
import logging
import random
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

import websockets

from .config import (
    WS_PORT, HTTP_PORT, API_BASE, API_KEY,
    CLIENT_CTX, USER_AGENTS,
)

log = logging.getLogger("omniflash.bridge")


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
        self._start_http_server()

        self._ws_server = await websockets.serve(
            self._on_connect, "127.0.0.1", WS_PORT
        )
        log.info("⚡ WebSocket server on ws://127.0.0.1:%d", WS_PORT)
        log.info("⚡ HTTP callback on http://127.0.0.1:%d", HTTP_PORT)
        log.info("⏳ Waiting for Chrome extension to connect...")

    async def wait_for_extension(self, timeout=90, max_retries=3):
        """Wait until extension connects and sends flow key.
        
        Phase 1: Wait for WebSocket connection from extension.
        Phase 2: If no token, auto-open/refresh Flow tab and wait for token.
        """
        # Phase 1: Wait for WS connection
        try:
            await asyncio.wait_for(self._wait_for_ws(), 30)
        except asyncio.TimeoutError:
            log.error("❌ Extension didn't connect in 30s")
            log.error("   Make sure Flow Agent extension is installed and enabled in Chrome")
            return False

        # If token already present, we're good
        if self._flow_key:
            return True

        # Phase 2: Extension connected but no token — auto-fix
        log.info("⚠️  Extension connected but no auth token — auto-fixing...")
        
        for attempt in range(1, max_retries + 1):
            log.info("🔄 Attempt %d/%d: Opening/refreshing Flow tab...", attempt, max_retries)
            await self._request_flow_tab()
            
            # Wait for token to arrive (token_captured message)
            token_arrived = await self._wait_for_token(20)
            if token_arrived:
                log.info("✅ Token captured after auto-fix!")
                return True
            
            log.warning("⏳ Token not captured yet...")

        log.error("❌ Could not get auth token after %d retries", max_retries)
        log.error("   Make sure you're logged into Google at labs.google/fx/tools/flow")
        return False

    async def _wait_for_ws(self):
        """Wait until a WebSocket connection is established."""
        while not self._ws:
            await asyncio.sleep(0.5)

    async def _wait_for_token(self, timeout):
        """Wait until a valid token is captured."""
        self._connected.clear()
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return self._flow_key is not None

    async def _request_flow_tab(self):
        """Ask extension to open or refresh a Flow tab."""
        if not self._ws:
            return
        try:
            log.info("📂 Requesting extension to open/refresh Flow tab...")
            await self._ws.send(json.dumps({"method": "open_flow_tab"}))
            # Wait for page to fully load before requesting token refresh
            await asyncio.sleep(8)
            log.info("🔄 Requesting token refresh from Flow tab...")
            await self._ws.send(json.dumps({"method": "refresh_flow_tab"}))
        except Exception as e:
            log.debug("Failed to request flow tab: %s", e)

    async def health_check(self):
        """Quick check if extension is ready with valid token."""
        if not self._ws or not self._flow_key:
            return False
        try:
            req_id = str(uuid.uuid4())
            future = self._loop.create_future()
            self._pending[req_id] = future
            await self._ws.send(json.dumps({
                "id": req_id,
                "method": "get_status",
            }))
            result = await asyncio.wait_for(future, timeout=5)
            self._pending.pop(req_id, None)
            return result.get("result", {}).get("flowKeyPresent", False)
        except Exception:
            self._pending.pop(req_id, None)
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
            req_id = data.get("id")
            if req_id and req_id in self._pending:
                if not self._pending[req_id].done():
                    self._pending[req_id].set_result(data)

    def handle_http_callback(self, data):
        """Called from HTTP thread when extension sends callback."""
        req_id = data.get("id")
        if req_id and req_id in self._pending:
            self._loop.call_soon_threadsafe(
                self._resolve_pending, req_id, data
            )
            return True
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

        url = f"{API_BASE}{url_path}?key={API_KEY}"
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
                    "origin": CLIENT_CTX["origin"],
                    "referer": CLIENT_CTX["origin"] + "/",
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
                pass

        server = HTTPServer(("127.0.0.1", HTTP_PORT), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    async def close(self):
        self._ws_server.close()
        await self._ws_server.wait_closed()
