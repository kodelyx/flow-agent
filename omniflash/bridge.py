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
    CLIENT_CTX, USER_AGENTS, API_REQUEST_TIMEOUT,
    MAX_CONCURRENT_REQUESTS, REQUEST_MIN_INTERVAL,
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
        # Late/orphan-response reconciliation: remember a small window of recent
        # requests so a response that arrives after its caller gave up is not
        # silently dropped but handed to the orphan handler instead.
        self._req_meta: dict[str, dict] = {}
        self._orphan_handler = None
        # The extension retries delivery until acked, so the same response may
        # arrive more than once. Track ids already resolved/recovered to make
        # delivery idempotent (no duplicate saves).
        self._seen_ids: dict[str, bool] = {}
        # Rate limiting: cap concurrent generations and space them out so we
        # don't trip Google's UNUSUAL_ACTIVITY throttle. Semaphore is created
        # lazily on the running loop (see _get_rate_limit).
        self._rate_sem: asyncio.Semaphore | None = None
        self._rate_lock: asyncio.Lock | None = None
        self._last_request_at: float = 0.0

    def _get_rate_limit(self):
        """Lazily build the concurrency semaphore + spacing lock on the active
        loop (they must be bound to the loop that awaits them)."""
        if self._rate_sem is None:
            self._rate_sem = asyncio.Semaphore(max(1, MAX_CONCURRENT_REQUESTS))
        if self._rate_lock is None:
            self._rate_lock = asyncio.Lock()
        return self._rate_sem, self._rate_lock

    def _mark_seen(self, req_id, max_keep=256):
        self._seen_ids[req_id] = True
        while len(self._seen_ids) > max_keep:
            oldest = next(iter(self._seen_ids))
            self._seen_ids.pop(oldest, None)

    def set_orphan_handler(self, handler):
        """Register async fn(data, meta) called when a response arrives for a
        request whose caller already timed out. Lets late-but-successful
        generations be recovered instead of discarded."""
        self._orphan_handler = handler

    def _remember_request(self, req_id, meta, max_keep=64):
        self._req_meta[req_id] = meta
        while len(self._req_meta) > max_keep:
            oldest = next(iter(self._req_meta))
            self._req_meta.pop(oldest, None)

    async def send_message(self, msg):
        if not self._ws:
            return
        try:
            if hasattr(self._ws, "send_text"):
                await self._ws.send_text(json.dumps(msg))
            else:
                await self._ws.send(json.dumps(msg))
        except Exception as e:
            log.warning("Failed to send message: %s", e)

    async def handle_fastapi_ws(self, ws):
        self._ws = ws
        log.info("Extension connected via FastAPI WebSocket!")
        self._connected.set()

        # Send callback config to extension
        import os
        space_id = os.environ.get("SPACE_ID")
        if space_id:
            author, name = space_id.split("/")
            subdomain = f"{author.lower()}-{name.lower()}".replace("_", "-")
            callback_url = f"https://{subdomain}.hf.space/api/ext/callback"
        else:
            callback_url = f"http://127.0.0.1:{os.environ.get('OPENAI_API_PORT', '8001')}/api/ext/callback"

        await self.send_message({
            "type": "callback_config",
            "secret": "flow_secret",
            "callback_url": callback_url
        })

        # Send current state + resend token if we have one
        await self.send_message({
            "type": "extension_ready",
            "flowKeyPresent": self._flow_key is not None,
        })
        if self._flow_key:
            await self.send_message({
                "type": "token_captured",
                "flowKey": self._flow_key
            })

        try:
            while True:
                raw = await ws.receive_text()
                data = json.loads(raw)
                await self._handle_message(data)
        except Exception as e:
            log.warning("FastAPI WebSocket disconnected: %s", e)
        finally:
            self._ws = None
            self._connected.clear()

    async def start(self):
        """Start WS server and HTTP callback server."""
        self._loop = asyncio.get_event_loop()
        self._start_http_server()

        self._ws_server = await websockets.serve(
            self._on_connect, "127.0.0.1", WS_PORT
        )
        log.info("WebSocket server on ws://127.0.0.1:%d", WS_PORT)
        log.info("HTTP callback on http://127.0.0.1:%d", HTTP_PORT)
        log.info("Waiting for Chrome extension to connect...")

    async def wait_for_extension(self, timeout=90, max_retries=3):
        """Wait until extension connects and sends flow key.

        Phase 1: Wait for WebSocket connection from extension.
        Phase 2: If no token, auto-open/refresh Flow tab and wait for token.
        """
        # Phase 1: Wait for WS connection
        try:
            await asyncio.wait_for(self._wait_for_ws(), 30)
        except asyncio.TimeoutError:
            log.error("Extension didn't connect in 30s")
            log.error("   Make sure Flow Agent extension is installed and enabled in Chrome")
            return False

        # If token already present, we're good
        if self._flow_key:
            return True

        # Phase 2: Extension connected but no token — auto-fix
        log.info("Extension connected but no auth token — auto-fixing...")

        for attempt in range(1, max_retries + 1):
            log.info("Attempt %d/%d: Opening/refreshing Flow tab...", attempt, max_retries)
            await self._request_flow_tab()

            # Wait for token to arrive (token_captured message)
            token_arrived = await self._wait_for_token(20)
            if token_arrived:
                log.info("Token captured after auto-fix!")
                return True

            log.warning("Token not captured yet...")

        log.error("Could not get auth token after %d retries", max_retries)
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
            log.info("Requesting extension to open/refresh Flow tab...")
            await self.send_message({"method": "open_flow_tab"})
            # Wait for page to fully load before requesting token refresh
            await asyncio.sleep(8)
            log.info("Requesting token refresh from Flow tab...")
            await self.send_message({"method": "refresh_flow_tab"})
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
            await self.send_message({
                "id": req_id,
                "method": "get_status",
            })
            result = await asyncio.wait_for(future, timeout=5)
            self._pending.pop(req_id, None)
            return result.get("result", {}).get("flowKeyPresent", False)
        except Exception:
            self._pending.pop(req_id, None)
            return False

    async def _on_connect(self, ws):
        self._ws = ws
        log.info("Extension connected!")
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
            first_time = self._flow_key is None
            self._flow_key = data.get("flowKey")
            if first_time:
                log.info("Auth token captured")
            else:
                log.debug("Auth token refreshed")
            self._connected.set()

        elif msg_type == "extension_ready":
            log.info("Extension ready (flowKey=%s)", "yes" if data.get("flowKeyPresent") else "no")
            if data.get("flowKeyPresent") and self._flow_key:
                self._connected.set()

        elif msg_type in ("pong", "ping"):
            if msg_type == "ping" and self._ws:
                await self.send_message({"type": "pong"})

        else:
            req_id = data.get("id")
            self._route_response(req_id, data)

    def _route_response(self, req_id, data):
        """Route an extension response. Fast path resolves the waiting future;
        if the caller already timed out, hand the response to the orphan
        handler so a late-but-successful generation isn't lost. Delivery is
        idempotent: a redelivered id is acknowledged but not acted on twice."""
        if not req_id:
            return
        fut = self._pending.get(req_id)
        if fut is not None:
            if not fut.done():
                self._mark_seen(req_id)
                fut.set_result(data)
            return
        # Duplicate of an already-handled response (extension retried after the
        # ack was lost) — acknowledge silently, don't recover it again.
        if req_id in self._seen_ids:
            return
        # No waiting future: caller already gave up. Try to recover it.
        self._mark_seen(req_id)
        meta = self._req_meta.pop(req_id, None)
        if self._orphan_handler is not None:
            handler = self._orphan_handler
            coro = handler(data, meta or {})
            if self._loop is not None:
                asyncio.ensure_future(coro, loop=self._loop)
        else:
            log.warning("Dropped orphan response for %s (no handler)", req_id)

    def handle_http_callback(self, data):
        """Called from HTTP thread when extension sends callback."""
        req_id = data.get("id")
        if req_id:
            # Ack known ids (waiting, recoverable, or already-seen duplicates)
            # so the extension's durable outbox stops retrying. Route it on the
            # loop thread; _route_response dedups and recovers as needed.
            if (req_id in self._pending or req_id in self._req_meta
                    or req_id in self._seen_ids):
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
        self._route_response(req_id, data)

    async def api_request(self, url_path, body, captcha_action="VIDEO_GENERATION", method="POST", timeout=None, meta=None):
        """Send API request through Chrome extension.

        Generation requests (those with a non-empty captcha_action) are rate
        limited: at most MAX_CONCURRENT_REQUESTS in flight and spaced at least
        REQUEST_MIN_INTERVAL seconds apart. Non-generation calls (polling,
        credits — captcha_action="") bypass the limiter so they stay responsive.
        """
        if not self._ws:
            return {"error": "Extension not connected"}

        # Only throttle credit/captcha-consuming generation calls.
        if captcha_action:
            sem, lock = self._get_rate_limit()
            async with sem:
                await self._space_out_requests(lock)
                return await self._do_api_request(url_path, body, captcha_action, method, timeout, meta)
        return await self._do_api_request(url_path, body, captcha_action, method, timeout, meta)

    async def _space_out_requests(self, lock):
        """Enforce a minimum gap between the starts of consecutive generation
        requests so bursts don't trip Google's UNUSUAL_ACTIVITY throttle."""
        if REQUEST_MIN_INTERVAL <= 0:
            return
        async with lock:
            now = self._loop.time()
            wait = self._last_request_at + REQUEST_MIN_INTERVAL - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = self._loop.time()

    async def _do_api_request(self, url_path, body, captcha_action, method, timeout, meta):
        req_id = str(uuid.uuid4())
        future = self._loop.create_future()
        self._pending[req_id] = future
        self._remember_request(req_id, {
            "captcha_action": captcha_action,
            "url_path": url_path,
            **(meta or {}),
        })

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
        await self.send_message(msg)

        try:
            result = await asyncio.wait_for(future, timeout=timeout or API_REQUEST_TIMEOUT)
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
