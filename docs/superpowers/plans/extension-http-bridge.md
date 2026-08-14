# Flow-Agent Extension HTTP/SSE Bridge Implementation Plan

> **For AI Agents & Developers:** Required sub-skills: use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to execute or review tasks step by step. Use markdown checkboxes (`- [x]` / `- [ ]`) to track progress.

**Working Directory:** Repository root (`flow-agent` / `flow-extension`)

**Goal:** Transform the communication between the Flow-Agent browser extension and the local Python backend from strictly relying on WebSocket (`ws://127.0.0.1:8001/ws`) to an HTTP-first bidirectional bridge (HTTP polling + optional SSE), ensuring stable connectivity across fingerprint browsers like Hubstudio, AdsPower, and standard Google Chrome.

**Architecture:** Maintain the existing message model ("backend sends command -> extension executes on Google Flow -> extension returns results"), while upgrading the transport layer:
1. **Registration:** Extension proactively sends `POST /api/ext/hello` to register its session and report the captured `flowKey` (token).
2. **Command Dispatch:** Extension polls `GET /api/ext/poll` every 1–2 seconds to pull queued execution commands.
3. **Result Callback:** Extension posts results back to `POST /api/ext/callback`.
4. **SSE Downlink (Optional):** Low-latency push via `GET /api/ext/events`.
5. **WebSocket Fallback:** WebSocket (`/ws`) is preserved as a fallback for maximum backwards compatibility.

**Tech Stack:** Chrome MV3 Extension (`background.js`), FastAPI, `flow_engine/bridge.py`, `flow_engine/http_bridge.py`, pytest, `chrome.alarms` / `fetch`.

---

## Repository Layout

```text
flow-agent/
├── .github/workflows/build.yml     # CI/CD and multi-platform build workflow
├── flow-agent/                     # Python Backend & CLI
│   ├── flow_engine/                # Core execution & bridge logic
│   │   ├── bridge.py               # Main bidirectional bridge router
│   │   ├── http_bridge.py          # In-memory HTTP session registry & command queue
│   │   ├── config.py               # Environment configuration & defaults
│   │   ├── media_store.py          # Media persistence & caching
│   │   └── upload.py               # Asset upload utilities
│   ├── flow_server/                # FastAPI application & endpoints
│   │   ├── app.py                  # FastAPI app factory
│   │   ├── api.py                  # API server bootstrap
│   │   ├── state.py                # Server state & lifecycle management
│   │   └── routes/                 # Modular API route handlers
│   │       ├── system.py           # Extension transport (/api/ext/*), /health, credits
│   │       ├── generation.py       # Flow video/image generation endpoints
│   │       ├── chat.py             # OpenAI-compatible chat completions
│   │       └── media.py            # Media file serving
│   ├── tests/                      # Pytest suite
│   │   ├── test_http_bridge.py     # HTTP registry unit tests
│   │   └── test_ext_http_api.py    # FastAPI extension endpoint tests
│   ├── pyproject.toml
│   └── main.py
├── flow-extension/                 # Chrome MV3 Extension
│   ├── background.js               # Service worker handling HTTP/WS bridge & polling
│   ├── manifest.json               # Extension manifest (MV3)
│   ├── content.js                  # Content script for token sniffing & DOM interaction
│   ├── injected.js                 # Network interception script
│   ├── popup.html / popup.js       # Extension status popup UI
│   └── config.js                   # Client-side configuration
├── docs/superpowers/plans/         # Engineering design & implementation plans
└── README.md                       # Documentation
```

---

## Background & Technical Constraints

### Previous Link Architecture
1. Backend `flow serve` listens on `http://127.0.0.1:8001`.
2. Extension establishes a connection to `ws://127.0.0.1:8001/ws`.
3. Backend sends commands via WebSocket: `api_request`, `trpc_request`, `upload_video`, `solve_captcha`, `get_status`, `open_flow_tab`, `refresh_flow_tab`.
4. Extension captures the Google Flow session token and returns `token_captured` / `extension_ready`.
5. Outbox responses with `id` could already be delivered via HTTP `POST /api/ext/callback`.

### Fingerprint Browser Compatibility Test Results
| Browser Environment | Local HTTP | Local WebSocket | Flow Token Capture |
|---------------------|------------|-----------------|--------------------|
| Standard Google Chrome | Works | Works | Yes |
| AdsPower | Works | Fails / Disconnects instantly | Yes |
| Hubstudio | Often fails on client WS | Fails | Yes |

**Key Takeaway:** "Extension connected" can no longer be defined strictly as "Active WebSocket object exists".  
**New Definition:** "Extension has sent a heartbeat/hello/poll within TTL (e.g., last 15–20s) AND holds a valid `flowKey`."

---

## Core Design Principles

1. **HTTP First, WebSocket Fallback:** (`EXT_TRANSPORT=auto|http|ws`, default `auto`).
2. **Backend Command Queue:** Commands reside safely in the backend memory queue until polled by the extension.
3. **Idempotency:** Commands and responses indexed by request `id` allow retries without duplicate execution.
4. **Unified Port:** Standardize all internal communication on port `8001`.
5. **Manifest Permissions:** Ensure `manifest.json` `host_permissions` include `http://127.0.0.1:8001/*` and `http://localhost:8001/*`.

---

## API Protocol Specifications

### A. Extension -> Backend

#### 1. Session Registration
`POST /api/ext/hello`
```json
{
  "type": "hello",
  "session_id": "client-uuid",
  "extension_version": "1.0.0",
  "flowKeyPresent": true,
  "flowKey": "ya29.a0AfH...",
  "capabilities": ["api_request", "trpc_request", "upload_video", "solve_captcha"]
}
```

Response:
```json
{
  "ok": true,
  "session_id": "client-uuid",
  "secret": "bearer-callback-secret",
  "callback_url": "/api/ext/callback",
  "poll_url": "/api/ext/poll",
  "poll_interval_ms": 1000,
  "events_url": "/api/ext/events"
}
```

#### 2. Command Polling
`GET /api/ext/poll?session_id=client-uuid`
- **Headers:** `Authorization: Bearer <secret>`
- **Response:**
```json
{
  "ok": true,
  "commands": [
    {
      "id": "req-uuid-1234",
      "method": "api_request",
      "params": {
        "url": "https://labs.google/fx/api/trpc/...",
        "body": { "..." : "..." }
      }
    }
  ],
  "server_time": 1784488000000
}
```

#### 3. Callback & Event Reporting
`POST /api/ext/callback`
- **Headers:** `Authorization: Bearer <secret>`
- **Body:** Handles command responses, `token_captured`, `extension_ready`, `ping`, and `media_urls_refresh`.

---

### B. Backend -> Extension Dispatch
In HTTP mode, `bridge.send_message(msg)` enqueues the payload into `ExtensionHttpRegistry`. The extension fetches and executes it on the next poll cycle.

---

### C. Health & Readiness Definition
```python
extension_connected = session_last_seen_within(15.0)
has_flow_key = bool(flow_key)
healthy = extension_connected and has_flow_key
```

Health endpoint (`GET /health`) reports:
```json
{
  "status": "healthy",
  "extension_connected": true,
  "has_flow_key": true,
  "transport": "http"
}
```

---

## Tasks & Implementation Status

### Task 0: Workspace Baseline & Directory Structure
- [x] Standardize repository structure into `flow-agent/` (Python backend) and `flow-extension/` (Chrome extension).
- [x] Configure Python dependencies via `pyproject.toml` and virtual environments.

---

### Task 1: In-Memory HTTP Session Registry & Command Queue
- **Files:** `flow-agent/flow_engine/http_bridge.py`, `flow-agent/tests/test_http_bridge.py`
- [x] Implement thread-safe `ExtensionHttpRegistry` with session TTL expiry.
- [x] Implement `hello()`, `touch()`, `enqueue()`, `poll()`, and `get_flow_key()`.
- [x] Unit test coverage in `tests/test_http_bridge.py`.

---

### Task 2: FastAPI HTTP Bridge Endpoints & Bridge Integration
- **Files:** `flow-agent/flow_server/routes/system.py`, `flow-agent/flow_engine/bridge.py`, `flow-agent/tests/test_ext_http_api.py`
- [x] Add `POST /api/ext/hello` route.
- [x] Add `GET /api/ext/poll` with Bearer token authentication.
- [x] Support HTTP outbox callback routing in `POST /api/ext/callback`.
- [x] Update `/health` endpoint to reflect HTTP/WebSocket transport status.

---

### Task 3: Extension HTTP Transport & Fallback Implementation
- **Files:** `flow-extension/manifest.json`, `flow-extension/background.js`, `flow-extension/popup.js`
- [x] Update `manifest.json` host permissions for `http://127.0.0.1:8001/*` and `http://localhost:8001/*`.
- [x] Implement `connectHttpAgent()` and `pollHttpCommands()` in `background.js`.
- [x] Add automatic fallback (`auto` mode: try HTTP hello/poll; fall back to WebSocket if unreachable).
- [x] Keep-alive alarms via `chrome.alarms` to prevent MV3 Service Worker sleep during active polling.

---

### Task 4: Configuration & Documentation
- **Files:** `flow-agent/flow_engine/config.py`, `flow-agent/.env`, `README.md`
- [x] Add configurable environment variables:
  - `EXT_TRANSPORT` (`auto` / `http` / `ws`)
  - `EXT_SESSION_TTL_SEC` (default: 20)
  - `EXT_POLL_INTERVAL_MS` (default: 1000)
  - `ENABLE_EXTENSION_WS` (default: 1)
- [x] Update `README.md` with instructions for running with fingerprint browsers.

---

### Task 5: End-to-End Testing & Verification Matrix
- [x] Automated pytest validation (`test_http_bridge.py`, `test_cli.py`, etc.).
- [x] Standard Google Chrome extension validation.
- [ ] AdsPower / Hubstudio fingerprint browser live environment verification.

---

## Risk Assessment & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| MV3 Service Worker Inactivity | Delayed command execution | Active `chrome.alarms` heartbeat trigger + backend pending queue |
| Fingerprint Browser Localhost Blocking | Extension unable to reach `127.0.0.1` | Support LAN IP bindings or optional proxy bypass configuration |
| Duplicate Delivery (HTTP + WS) | Redundant generation requests | Command ID deduplication & strict single active transport selection |
| Legacy Extension Compatibility | Older client versions failing | Retain `/ws` WebSocket endpoint alongside HTTP bridge |

---

## Verification Criteria
1. **Standard Chrome:** Extension connects via HTTP bridge, captures token, and successfully handles generation commands.
2. **Fingerprint Browsers:** Operates seamlessly via HTTP polling without requiring WebSocket support.
3. **Health Status:** `GET /health` accurately reports active `transport` mode (`http`, `ws`, or `none`).
4. **Fallback:** Switching `EXT_TRANSPORT=ws` continues to function as expected.
