"""Omni Flash — ExtensionBridge.

WebSocket + HTTP server that communicates with the Chrome extension.
Handles auth token capture, API proxying, and request/response routing.
"""

import asyncio
import hmac
import json
import logging
import random
import secrets
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

import websockets

from .config import (
    WS_PORT, HTTP_PORT, API_BASE, API_KEY,
    CLIENT_CTX, USER_AGENTS, API_REQUEST_TIMEOUT,
    MAX_CONCURRENT_REQUESTS, REQUEST_MIN_INTERVAL,
)
from .http_bridge import ExtensionHttpRegistry

log = logging.getLogger("omniflash.bridge")


class ExtensionBridge:
    """WebSocket server that Chrome extension connects to."""

    def __init__(self, session_ttl_sec: float = 15.0):
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
        # Per-process callback credential shared only with the connected extension.
        # Keep this out of configuration, logs, and error responses.
        self._callback_secret = secrets.token_urlsafe(32)
        # HTTP hello/poll transport (preferred for fingerprint browsers).
        self.http_registry = ExtensionHttpRegistry(session_ttl_sec=session_ttl_sec)

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

    def is_extension_connected(self) -> bool:
        """True when an HTTP session is online or a WebSocket is open."""
        if self.http_registry.has_online_session():
            return True
        return self._ws is not None

    def has_flow_key(self) -> bool:
        if self._flow_key:
            return True
        return bool(self.http_registry.get_flow_key())

    def active_transport(self) -> str:
        """Return preferred active transport: http | ws | none."""
        if self.http_registry.has_online_session():
            return "http"
        if self._ws is not None:
            return "ws"
        return "none"

    def enqueue_http_command(self, command: dict) -> bool:
        """Enqueue a command for the latest online HTTP session."""
        return self.http_registry.enqueue(None, command)

    def register_http_session(
        self,
        session_id: str,
        flow_key: str | None = None,
        *,
        secret: str | None = None,
        meta: dict | None = None,
    ) -> dict:
        """Register/refresh an HTTP extension session and mirror flow key."""
        resolved_secret = secret or self._callback_secret
        out = self.http_registry.hello(
            session_id=session_id,
            flow_key=flow_key,
            secret=resolved_secret,
            meta=meta,
        )
        if flow_key:
            self._flow_key = flow_key
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._connected.set)
            else:
                self._connected.set()
        return out

    def poll_http_commands(self, session_id: str, max_commands: int = 10) -> dict:
        return self.http_registry.poll(session_id, max_commands=max_commands)

    def verify_http_session_authorization(
        self, session_id: str, authorization: str | None
    ) -> bool:
        return self.http_registry.verify_authorization(session_id, authorization)

    async def send_message(self, msg):
        # Prefer HTTP command queue when an extension is polling.
        if self.http_registry.has_online_session():
            self.http_registry.enqueue(None, msg)
            return
        if not self._ws:
            return
        try:
            if hasattr(self._ws, "send_text"):
                await self._ws.send_text(json.dumps(msg))
            else:
                await self._ws.send(json.dumps(msg))
        except Exception as e:
            log.warning("Failed to send message: %s", e)

    def _callback_config(self, callback_url):
        return {
            "type": "callback_config",
            "secret": self._callback_secret,
            "callback_url": callback_url,
        }

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

        await self.send_message(self._callback_config(callback_url))

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
        """Start optional standalone WS/HTTP servers for non-FastAPI callers."""
        self._loop = asyncio.get_running_loop()
        self._ws_server = None
        try:
            self._start_http_server()
            log.info("HTTP callback on http://127.0.0.1:%d", HTTP_PORT)
        except OSError as error:
            log.warning("Standalone HTTP callback server unavailable: %s", error)

        try:
            self._ws_server = await websockets.serve(
                self._on_connect, "127.0.0.1", WS_PORT
            )
            log.info("WebSocket server on ws://127.0.0.1:%d", WS_PORT)
        except OSError as error:
            # FastAPI mode uses /ws on the API port; standalone WS is optional.
            log.warning("Standalone WebSocket server unavailable: %s", error)

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
        """Wait until WebSocket or HTTP session is established."""
        while not self.is_extension_connected():
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
        if not self.is_extension_connected():
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
        if not self.is_extension_connected() or not self.has_flow_key():
            return False
        # HTTP mode: presence of recent session + flow key is enough health.
        if self.active_transport() == "http":
            return True
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
        await self.send_message(self._callback_config(
            f"http://127.0.0.1:{HTTP_PORT}/api/ext/callback"
        ))
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

    def verify_callback_authorization(self, authorization):
        """Constant-time verification for the callback Bearer credential."""
        candidate = ""
        if isinstance(authorization, str):
            scheme, separator, value = authorization.partition(" ")
            if separator and scheme.lower() == "bearer":
                candidate = value.strip()
        return hmac.compare_digest(candidate, self._callback_secret)

    def handle_http_callback(self, data):
        """Called from HTTP thread when extension sends callback."""
        req_id = data.get("id")
        if req_id:
            # Ack known ids (waiting, recoverable, or already-seen duplicates)
            # so the extension's durable outbox stops retrying. Route it on the
            # loop thread; _route_response dedups and recovers as needed.
            if (req_id in self._pending or req_id in self._req_meta
                    or req_id in self._seen_ids):
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(
                        self._resolve_pending, req_id, data
                    )
                else:
                    self._resolve_pending(req_id, data)
                return True
        msg_type = data.get("type")
        if msg_type == "token_captured":
            self._flow_key = data.get("flowKey")
            session_id = data.get("session_id") or data.get("sessionId")
            if session_id and self._flow_key:
                self.http_registry.hello(
                    session_id=str(session_id),
                    flow_key=self._flow_key,
                    secret=self._callback_secret,
                )
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._connected.set)
            else:
                self._connected.set()
            return True
        if msg_type in ("extension_ready", "ping", "media_urls_refresh"):
            session_id = data.get("session_id") or data.get("sessionId")
            if session_id:
                self.http_registry.touch(str(session_id))
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
        if not self.is_extension_connected():
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

    def _make_http_handler(self):
        """Build the authenticated standalone callback request handler."""
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, status, payload):
                encoded = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self):
                if self.path != "/api/ext/callback":
                    self._send_json(404, {"ok": False})
                    return

                authorization = self.headers.get("Authorization")
                if authorization is None:
                    self._send_json(401, {"ok": False})
                    return
                if not bridge.verify_callback_authorization(authorization):
                    self._send_json(403, {"ok": False})
                    return

                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length)) if length else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    self._send_json(400, {"ok": False})
                    return

                accepted = bridge.handle_http_callback(body)
                self._send_json(200 if accepted else 404, {"ok": bool(accepted)})

            def do_GET(self):
                if self.path == "/health":
                    self._send_json(200, {
                        "status": "ok",
                        "extension_connected": bridge.is_extension_connected(),
                        "has_flow_key": bridge.has_flow_key(),
                        "transport": bridge.active_transport(),
                    })
                else:
                    self._send_json(404, {"ok": False})

            def do_OPTIONS(self):
                # This loopback endpoint is not a public cross-origin API.
                self._send_json(405, {"ok": False})

            def log_message(self, *args):
                pass

        return Handler

    def _start_http_server(self):
        """Start HTTP server for extension callbacks (runs in thread)."""
        server = HTTPServer(("127.0.0.1", HTTP_PORT), self._make_http_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    async def close(self):
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None
