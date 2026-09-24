# Flow-Agent Architecture & Roadmap Plan

> **For AI Agents & Developers:** Use markdown checkboxes (`- [x]` / `- [ ]`) to track architecture milestones, planned features, and validation criteria.

**Working Directory:** `flow-agent/`

---

## 1. System Vision & Architecture

Transform Google Flow image and video generation into a high-throughput, programmable local daemon with:
1. **Engine Layer (`bin/flow`)**: Preflight-free request bursts, automatic account rotation, token refreshes, and SQLite persistence.
2. **Package Layer (`flow_agent/`)**: Clean, modular Python package (`FlowEngine`, `app`, `create_sse_app`).
3. **OpenAI Compatibility**: Complete drop-in emulation for standard OpenAI SDKs (`/v1/images/generations`, `/v1/chat/completions`).
4. **Agent Integration**: Anthropic Model Context Protocol (MCP 2024-11-05) over Stdio and SSE.

---

## 2. Milestone Progress Tracker

### Phase 1: Core Engine & Subprocess Bridge
- [x] Integrate compiled `flow-go` binary into `flow-agent/bin/flow`.
- [x] Zero-preflight request bursts implemented in Go engine.
- [x] Multi-account cookie syncing (`cookies/account_*.json`).
- [x] SQLite database schema initialized (`data/flow.db`).
- [x] Python `FlowEngine` async subprocess bridge (`flow_agent/engine.py`).
- [x] Trailing JSON extractor (`extract_json`) anchored to stdout delimiters.
- [x] Read-only SQLite connector (`file:...flow.db?mode=ro`).

### Phase 2: REST API & OpenAI Emulation
- [x] FastAPI application factory (`flow_agent/api.py`).
- [x] Native Flow routes (`/api/v1/image`, `/api/v1/video`, `/api/v1/balance`, `/api/v1/stats`, `/api/v1/projects`).
- [x] OpenAI Images Generations endpoint (`/v1/images/generations`) with size-to-aspect mapping.
- [x] OpenAI Chat Completions endpoint (`/v1/chat/completions`) with markdown rendering and SSE streams.
- [x] Media streaming endpoint (`/api/v1/media/{filename}`) with strict path traversal prevention.
- [x] Comprehensive error mapping (502 for engine refusals, 400 for bad parameters).

### Phase 3: Model Context Protocol (MCP) Server
- [x] Pure JSON-RPC protocol implementation (`flow_agent/mcp_server.py`).
- [x] Stdio transport for local AI assistants (Claude Desktop, Cursor, Antigravity).
- [x] SSE transport over HTTP for distributed web clients.
- [x] 5 Tool schemas defined (`flow_generate_image`, `flow_generate_video`, `flow_get_balance`, `flow_get_stats`, `flow_upload_video`).
- [x] Handshake negotiation (`protocolVersion: 2024-11-05`).

### Phase 4: Packaging & Test Automation
- [x] Reorganized flat root into clean `flow_agent/` package.
- [x] PEP 621 compliant `pyproject.toml` with console scripts.
- [x] Relocated `conftest.py` into `tests/conftest.py`.
- [x] 74/74 unit and integration tests passing offline in <0.5 seconds.
- [x] Comprehensive documentation suite (`docs/`).

---

## 3. Future Roadmap

- [ ] **Docker Containerization**: Multi-stage `Dockerfile` and `docker-compose.yml` for cloud deployments.
- [ ] **Optional API Key Authentication**: Bearer token middleware for public-facing deployments.
- [ ] **Web UI Dashboard**: Built-in visual prompt testing playground served on `/`.
