"""Omni Flash — ExtensionBridge.

WebSocket + HTTP server that communicates with the Chrome extension.
Handles auth token capture, API proxying, and request/response routing.
Supports multiple concurrent extension clients (multi-PC/multi-browser setup).
"""

import time as _time
import contextvars

import asyncio
import json
import logging
import random
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

import websockets

target_client_id_var = contextvars.ContextVar("target_client_id", default=None)

from .config import (
    WS_PORT, HTTP_PORT, API_BASE, API_KEY,
    CLIENT_CTX, USER_AGENTS, API_REQUEST_TIMEOUT,
    MAX_CONCURRENT_REQUESTS, REQUEST_MIN_INTERVAL,
)

log = logging.getLogger("omniflash.bridge")


class ExtensionBridge:
    """WebSocket server that Chrome extensions connect to."""

    # Seconds to wait after server boot before allowing generation requests.
    # Prevents UNUSUAL_ACTIVITY errors caused by 40+ browsers reconnecting
    # simultaneously and the first request landing before Google stabilises.
    STARTUP_COOLDOWN = 10

    def __init__(self):
        self._clients: dict[str, websockets.WebSocketServerProtocol] = {}  # client_id -> WebSocket
        self._tokens: dict[str, str] = {}         # client_id -> flowKey
        self._states: dict[str, str] = {}         # client_id -> state ('idle' | 'running')
        self._tiers: dict[str, str] = {}          # client_id -> sku ('G1_FREEMIUM' free | 'G1_TIER1' pro)
        self._credits: dict[str, int] = {}        # client_id -> last-known credit balance
        self._pending: dict[str, asyncio.Future] = {}
        self._connected = asyncio.Event()
        self._loop = None
        self._boot_time = _time.monotonic()  # for startup cooldown

        # Late/orphan-response reconciliation
        self._req_meta: dict[str, dict] = {}
        self._orphan_handler = None
        self._seen_ids: dict[str, bool] = {}

        # Client-specific rate limiting
        self._client_sems: dict[str, asyncio.Semaphore] = {}
        self._client_locks: dict[str, asyncio.Lock] = {}
        self._client_last_request_at: dict[str, float] = {}

    @property
    def _ws(self):
        """Compatibility property for legacy code that accesses bridge._ws.
        Returns the first connected WebSocket client."""
        for ws in self._clients.values():
            if ws:
                return ws
        return None

    @property
    def _flow_key(self):
        """Compatibility property for legacy code that accesses bridge._flow_key.
        Returns the first captured token in the pool."""
        for tok in self._tokens.values():
            if tok:
                return tok
        return None

    def _get_client_rate_limit(self, client_id):
        """Build or retrieve client-specific concurrency semaphore and spacing lock."""
        if client_id not in self._client_sems:
            self._client_sems[client_id] = asyncio.Semaphore(max(1, MAX_CONCURRENT_REQUESTS))
        if client_id not in self._client_locks:
            self._client_locks[client_id] = asyncio.Lock()
        return self._client_sems[client_id], self._client_locks[client_id]

    def _mark_seen(self, req_id, max_keep=256):
        self._seen_ids[req_id] = True
        while len(self._seen_ids) > max_keep:
            oldest = next(iter(self._seen_ids))
            self._seen_ids.pop(oldest, None)

    def set_orphan_handler(self, handler):
        self._orphan_handler = handler

    def _remember_request(self, req_id, meta, max_keep=64):
        self._req_meta[req_id] = meta
        while len(self._req_meta) > max_keep:
            oldest = next(iter(self._req_meta))
            self._req_meta.pop(oldest, None)

    def _is_free(self, cid) -> bool:
        """Free-tier client? Freemium accounts are spent first so paid/pro
        credits are preserved. Unknown tier is treated as free (spend it early)."""
        return self._tiers.get(cid, "G1_FREEMIUM") != "G1_TIER1"

    def _select_client(self) -> str | None:
        """Selects the best connected client to route a request to.
        Prioritizes:
          1. Idle clients with captured tokens (free tier before pro).
          2. Any connected client with a captured token (free before pro).
          3. Any connected client.
        Free (G1_FREEMIUM) clients are always drained before paid (G1_TIER1)
        ones so pro credits are only touched once free credits run out.
        """
        connected_ids = list(self._clients.keys())
        if not connected_ids:
            return None

        def _pick(pool):
            """Prefer free-tier clients in the pool; fall back to pro."""
            free = [c for c in pool if self._is_free(c)]
            if free:
                return random.choice(free)
            return random.choice(pool) if pool else None

        # Filter clients with active tokens
        with_tokens = [cid for cid in connected_ids if self._tokens.get(cid)]

        # Filter idle clients with tokens
        idle_with_tokens = [cid for cid in with_tokens if self._states.get(cid) == "idle"]
        if idle_with_tokens:
            return _pick(idle_with_tokens)

        if with_tokens:
            return _pick(with_tokens)

        return random.choice(connected_ids)

    def _select_client_for_cost(self, cost: int) -> str | None:
        """Pick a client that can actually afford a `cost`-credit job.

        Same free-before-pro priority as _select_client, but additionally skips
        any client whose last-known balance is below `cost`. This prevents
        routing a 15-credit video to a browser that only has 5 credits left
        while a 50-credit browser sits idle. Clients with no cached balance yet
        are treated as affordable (optimistic — the real credit gate re-checks
        on the pinned client before generating).
        """
        if cost <= 0:
            return self._select_client()

        connected_ids = list(self._clients.keys())
        if not connected_ids:
            return None

        def _affordable(cid):
            bal = self._credits.get(cid)
            return bal is None or bal >= cost

        def _pick(pool):
            pool = [c for c in pool if _affordable(c)]
            if not pool:
                return None
            free = [c for c in pool if self._is_free(c)]
            if free:
                # Prefer the free client with the MOST credits so it drains cleanly.
                known = [c for c in free if self._credits.get(c) is not None]
                if known:
                    return max(known, key=lambda c: self._credits[c])
                return random.choice(free)
            known = [c for c in pool if self._credits.get(c) is not None]
            if known:
                return max(known, key=lambda c: self._credits[c])
            return random.choice(pool)

        with_tokens = [cid for cid in connected_ids if self._tokens.get(cid)]
        idle_with_tokens = [cid for cid in with_tokens if self._states.get(cid) == "idle"]

        pick = _pick(idle_with_tokens)
        if pick:
            return pick
        pick = _pick(with_tokens)
        if pick:
            return pick
        # Nobody provably affords it → fall back to normal selection (gate will 402).
        return self._select_client()

    async def send_message_to(self, client_id, msg):
        """Send message to a specific client in the pool."""
        ws = self._clients.get(client_id)
        if not ws:
            log.warning("Client %s not connected", client_id)
            return False
        try:
            if hasattr(ws, "send_text"):
                await ws.send_text(json.dumps(msg))
            else:
                await ws.send(json.dumps(msg))
            return True
        except Exception as e:
            log.warning("Failed to send message to client %s: %s", client_id, e)
            return False

    async def send_message(self, msg):
        """Broadcast message to all connected clients (legacy fallback)."""
        for cid in list(self._clients.keys()):
            await self.send_message_to(cid, msg)

    async def handle_fastapi_ws(self, ws):
        """Handle FastAPI WebSocket connection session."""
        # Setup temporary client ID until extension self-registers
        client_id = f"client_{uuid.uuid4().hex[:6]}"

        import os
        space_id = os.environ.get("SPACE_ID")
        if space_id:
            author, name = space_id.split("/")
            subdomain = f"{author.lower()}-{name.lower()}".replace("_", "-")
            callback_url = f"https://{subdomain}.hf.space/api/ext/callback"
        else:
            callback_url = f"http://{ws.url.netloc}/api/ext/callback"

        # Send callback config
        try:
            await ws.send_text(json.dumps({
                "type": "callback_config",
                "secret": "flow_secret",
                "callback_url": callback_url
            }))
        except Exception as e:
            log.warning("FastAPI WS initial setup failed: %s", e)
            return

        try:
            while True:
                raw = await ws.receive_text()
                data = json.loads(raw)
                client_id = await self._handle_message_with_client(data, ws, client_id)
        except Exception as e:
            log.warning("FastAPI WebSocket disconnected for client %s: %s", client_id, e)
        finally:
            self._clients.pop(client_id, None)
            self._tokens.pop(client_id, None)
            self._states.pop(client_id, None)
            self._tiers.pop(client_id, None)
            self._credits.pop(client_id, None)
            self._client_sems.pop(client_id, None)
            self._client_locks.pop(client_id, None)
            self._client_last_request_at.pop(client_id, None)
            if not self._clients:
                self._connected.clear()

    async def start(self):
        """Start WS server and HTTP callback server."""
        self._loop = asyncio.get_event_loop()
        self._start_http_server()

        # Bind to 0.0.0.0 to allow other PCs on local network to connect
        self._ws_server = await websockets.serve(
            self._on_connect, "0.0.0.0", WS_PORT
        )
        log.info("WebSocket server on ws://0.0.0.0:%d", WS_PORT)
        log.info("HTTP callback on http://0.0.0.0:%d", HTTP_PORT)
        log.info("Waiting for Chrome extensions to connect...")

    async def wait_for_extension(self, timeout=90, max_retries=3, auto_fix=True):
        """Wait until at least one extension connects and captures a token."""
        start_time = self._loop.time()
        while not self._clients:
            if self._loop.time() - start_time > timeout:
                log.error("No extensions connected within %ds", timeout)
                return False
            await asyncio.sleep(0.5)

        # Wait briefly for cached token to arrive
        start_time = self._loop.time()
        while not any(self._tokens.values()):
            if self._loop.time() - start_time > 2.0:
                break
            await asyncio.sleep(0.2)

        if any(self._tokens.values()):
            return True

        if not auto_fix:
            return any(self._tokens.values())

        log.info("Extensions connected but no auth tokens — requesting flow tabs...")
        for cid in list(self._clients.keys()):
            if not self._tokens.get(cid):
                await self._request_flow_tab_for(cid)

        # Wait for a token to arrive after requesting flow tabs
        start_time = self._loop.time()
        while not any(self._tokens.values()):
            if self._loop.time() - start_time > 20.0:
                break
            await asyncio.sleep(0.5)

        return any(self._tokens.values())

    async def _wait_for_ws(self):
        while not self._clients:
            await asyncio.sleep(0.5)

    async def _wait_for_token(self, timeout):
        self._connected.clear()
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return any(self._tokens.values())

    async def _request_flow_tab_for(self, client_id):
        """Ask specific extension to open or refresh a Flow tab."""
        if client_id not in self._clients:
            return
        if self._tokens.get(client_id):
            return
        try:
            log.info("Requesting client %s to open/refresh Flow tab...", client_id)
            await self.send_message_to(client_id, {"method": "open_flow_tab"})
            await asyncio.sleep(8)
            await self.send_message_to(client_id, {"method": "refresh_flow_tab"})
        except Exception as e:
            log.debug("Failed to request flow tab for client %s: %s", client_id, e)

    async def _force_refresh_client(self, client_id):
        """Force a client to reload its Flow tab and re-capture a fresh token,
        bypassing the extension's 50-min freshness cache. Used to self-heal a
        stale token (Google invalidates via inactivity before the cache expires).
        """
        if client_id not in self._clients:
            return
        try:
            log.info("Force-refreshing Flow tab for client %s (stale token self-heal)...", client_id)
            self._tokens.pop(client_id, None)
            await self.send_message_to(client_id, {"method": "force_refresh", "force": True})
            await asyncio.sleep(6)
        except Exception as e:
            log.debug("Force-refresh failed for client %s: %s", client_id, e)

    async def _request_flow_tab(self):
        """Fallback that triggers open_flow_tab on all connected clients."""
        for cid in list(self._clients.keys()):
            await self._request_flow_tab_for(cid)

    async def health_check(self):
        """Quick check if at least one extension is ready with valid token."""
        active_clients = [cid for cid in self._clients if self._tokens.get(cid)]
        return len(active_clients) > 0

    async def _on_connect(self, ws):
        """Handle raw WebSocket connections."""
        client_id = f"client_{uuid.uuid4().hex[:6]}"
        log.info("Extension connected via raw WebSocket!")
        try:
            async for raw in ws:
                data = json.loads(raw)
                client_id = await self._handle_message_with_client(data, ws, client_id)
        except Exception as e:
            log.warning("Raw WebSocket disconnected for client %s: %s", client_id, e)
        finally:
            self._clients.pop(client_id, None)
            self._tokens.pop(client_id, None)
            self._states.pop(client_id, None)
            self._tiers.pop(client_id, None)
            self._credits.pop(client_id, None)
            self._client_sems.pop(client_id, None)
            self._client_locks.pop(client_id, None)
            self._client_last_request_at.pop(client_id, None)
            if not self._clients:
                self._connected.clear()

    async def _handle_message_with_client(self, data, ws, current_client_id):
        msg_type = data.get("type")

        if msg_type == "extension_ready":
            client_id = data.get("clientId") or current_client_id
            if client_id != current_client_id:
                self._clients.pop(current_client_id, None)
                self._tokens.pop(current_client_id, None)
                self._states.pop(current_client_id, None)
                self._tiers.pop(current_client_id, None)
                self._credits.pop(current_client_id, None)
            self._clients[client_id] = ws
            self._states[client_id] = "idle"
            log.info("Extension client ready: %s (total: %d)", client_id, len(self._clients))

            # If client connects without a token, trigger an automatic flow tab refresh in the background
            if not data.get("flowKeyPresent"):
                log.info("Client %s has no cached token. Triggering auto token refresh...", client_id)
                asyncio.create_task(self._request_flow_tab_for(client_id))
            return client_id

        elif msg_type == "token_captured":
            flow_key = data.get("flowKey")
            self._tokens[current_client_id] = flow_key
            log.info("Token captured for client: %s", current_client_id)
            self._connected.set()
            return current_client_id

        elif msg_type in ("pong", "ping"):
            if msg_type == "ping":
                try:
                    await ws.send(json.dumps({"type": "pong"}))
                except Exception:
                    pass
            return current_client_id

        else:
            req_id = data.get("id")
            self._route_response(req_id, data)
            return current_client_id

    def _resolve_pending(self, req_id, data):
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
            client_id = data.get("clientId") or next(iter(self._clients.keys()), None)
            if client_id:
                self._tokens[client_id] = data.get("flowKey")
                self._loop.call_soon_threadsafe(self._connected.set)
            return True
        return False

    def _get_global_lock(self):
        """Build or retrieve global concurrency lock for IP-wide spacing."""
        if not hasattr(self, "_global_lock"):
            self._global_lock = asyncio.Lock()
            self._last_global_req_at = 0.0
        return self._global_lock

    async def _space_out_global_requests(self):
        """Ensure a minimum gap between requests across the entire client pool (IP level)."""
        import os
        global_interval = float(os.environ.get("GLOBAL_REQUEST_MIN_INTERVAL", "3.5"))
        lock = self._get_global_lock()
        async with lock:
            now = self._loop.time()
            wait = self._last_global_req_at + global_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_global_req_at = self._loop.time()

    def _get_global_sem(self):
        """Build or retrieve global request semaphore to limit parallel execution across all nodes."""
        if not hasattr(self, "_global_sem"):
            self._global_sem = asyncio.Semaphore(max(1, MAX_CONCURRENT_REQUESTS))
        return self._global_sem

    async def _wait_startup_cooldown(self):
        """Block until the startup cooldown has elapsed.
        Prevents burst requests right after server boot when all browsers
        reconnect simultaneously."""
        elapsed = _time.monotonic() - self._boot_time
        remaining = self.STARTUP_COOLDOWN - elapsed
        if remaining > 0:
            log.info("Startup cooldown: waiting %.1fs before first request...", remaining)
            await asyncio.sleep(remaining)

    async def api_request(self, url_path, body, captcha_action="VIDEO_GENERATION", method="POST", timeout=None, meta=None, client_id=None):
        """Send API request through a selected Chrome extension."""
        if not client_id:
            client_id = target_client_id_var.get() or self._select_client()
        if not client_id:
            return {"error": "No extensions connected"}

        log.info("Routing request to client: %s (total connected: %d)", client_id, len(self._clients))

        result = await self._run_api_request(client_id, url_path, body, captcha_action, method, timeout, meta)

        # Self-heal on a stale token: a 401 means Google invalidated this client's
        # token via inactivity. Force-refresh the tab and retry once on the same
        # client; if it still 401s, fail over to a different client.
        if self._is_unauthenticated(result):
            log.warning("Client %s returned 401 UNAUTHENTICATED — force-refreshing and retrying", client_id)
            await self._force_refresh_client(client_id)
            result = await self._run_api_request(client_id, url_path, body, captcha_action, method, timeout, meta)

            if self._is_unauthenticated(result):
                fallback = self._select_client_excluding(client_id)
                if fallback:
                    log.warning("Client %s still 401 after refresh — failing over to %s", client_id, fallback)
                    result = await self._run_api_request(fallback, url_path, body, captcha_action, method, timeout, meta)
        return result

    @staticmethod
    def _is_unauthenticated(result) -> bool:
        """True if an extension response indicates a 401/expired-token error."""
        if not isinstance(result, dict):
            return False
        if result.get("status") == 401:
            return True
        data = result.get("data")
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and (err.get("code") == 401 or err.get("status") == "UNAUTHENTICATED"):
                return True
        return False

    def _select_client_excluding(self, exclude_id) -> str | None:
        """Pick another connected client with a token, skipping exclude_id."""
        candidates = [c for c in self._clients if c != exclude_id and self._tokens.get(c)]
        if not candidates:
            return None
        free = [c for c in candidates if self._is_free(c)]
        return random.choice(free) if free else random.choice(candidates)

    async def _run_api_request(self, client_id, url_path, body, captcha_action, method, timeout, meta):
        """Execute a single request attempt against one client, applying the
        global + per-client rate limiting when a captcha action is involved."""
        if captcha_action:
            # Wait for startup cooldown if server just booted
            await self._wait_startup_cooldown()
            # Space out requests globally to protect against IP rate-limiting
            await self._space_out_global_requests()

            global_sem = self._get_global_sem()
            # Enforce global active slots (e.g. max 5 generation requests in flight)
            async with global_sem:
                sem, lock = self._get_client_rate_limit(client_id)
                async with sem:
                    await self._space_out_client_requests(client_id, lock)
                    return await self._do_api_request(client_id, url_path, body, captcha_action, method, timeout, meta)
        return await self._do_api_request(client_id, url_path, body, captcha_action, method, timeout, meta)

    async def _space_out_client_requests(self, client_id, lock):
        """Enforce a minimum gap between consecutive requests per client."""
        if REQUEST_MIN_INTERVAL <= 0:
            return
        async with lock:
            now = self._loop.time()
            last_req = self._client_last_request_at.get(client_id, 0.0)
            wait = last_req + REQUEST_MIN_INTERVAL - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._client_last_request_at[client_id] = self._loop.time()

    async def _do_api_request(self, client_id, url_path, body, captcha_action, method, timeout, meta):
        req_id = str(uuid.uuid4())
        future = self._loop.create_future()
        self._pending[req_id] = future
        self._remember_request(req_id, {
            "captcha_action": captcha_action,
            "url_path": url_path,
            "client_id": client_id,
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

        self._states[client_id] = "running"
        await self.send_message_to(client_id, msg)

        try:
            result = await asyncio.wait_for(future, timeout=timeout or API_REQUEST_TIMEOUT)
            if isinstance(result, dict):
                result["_client_id"] = client_id
                # Cache the account tier from any response that carries it (e.g.
                # a credits check) so _select_client can drain free before pro.
                rdata = result.get("data")
                if isinstance(rdata, dict) and rdata.get("sku"):
                    self._tiers[client_id] = rdata["sku"]
                if isinstance(rdata, dict) and "credits" in rdata:
                    try:
                        self._credits[client_id] = int(rdata["credits"])
                    except (TypeError, ValueError):
                        pass
            return result
        except asyncio.TimeoutError:
            return {"error": "TIMEOUT", "_client_id": client_id}
        finally:
            self._pending.pop(req_id, None)
            if client_id in self._states:
                self._states[client_id] = "idle"

    def _start_http_server(self):
        """Start HTTP server for extension callbacks on 0.0.0.0."""
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
                    # Calculate active clients status
                    active_clients = list(bridge._clients.keys())
                    self.wfile.write(json.dumps({
                        "status": "healthy" if len(active_clients) > 0 else "offline",
                        "extension_connected": len(active_clients) > 0,
                        "clients": [{"id": cid, "state": bridge._states.get(cid), "has_token": bridge._tokens.get(cid) is not None} for cid in active_clients]
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

        # Bind to 0.0.0.0 to enable callback routing from different devices
        server = HTTPServer(("0.0.0.0", HTTP_PORT), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    async def close(self):
        self._ws_server.close()
        await self._ws_server.wait_closed()
