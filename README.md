# ⚡ Flow Agent

A programmable, **OpenAI-compatible** image & video generation API + CLI on top of **Google Flow (Google Labs)** — plus an **MCP server** so Claude (and other MCP clients) can generate media directly.

It works by bridging to the **Flow Agent Chrome extension** over WebSocket, executing commands inside a logged-in Google Flow browser session.

> **Requirement:** Chrome with the Flow Agent extension installed and logged in at `labs.google/fx/tools/flow`. The extension is the generation engine — the backend just drives it. (No Docker required.)

---

## ⚡ Easy setup (macOS) — one command

On a new Mac, just run:

```bash
./setup.sh
```

That single command:
1. Installs the `flow` + `flow-mcp` CLI,
2. Makes the backend **start automatically on every login** (and restart itself if it ever crashes).

Then connect your AI client to the MCP server (Claude Desktop / Cursor / Cline / Antigravity / …) — copy-paste snippets for each are in **[MCP.md](MCP.md)**. Open Chrome at `labs.google/fx/tools/flow` (logged in, Flow Agent extension installed), and you're done. To turn off auto-start later: `./uninstall.sh`.

> Needs [`uv`](https://astral.sh/uv) (or `pipx`). If you don't have it: `curl -LsSf https://astral.sh/uv/install.sh | sh`, then re-run `./setup.sh`.

---

## 🛠️ Manual install

Install as a normal CLI tool (isolated environment, `flow` on your PATH):

```bash
# with uv (recommended)
uv tool install .

# or with pipx
pipx install .
```

For local development instead of an isolated install:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Then set your Google Flow project in `config.env` (all other defaults work out of the box):

```bash
# edit config.env → set DEFAULT_PROJECT to your labs.google project ID
```

---

## 🚀 CLI Usage

```bash
flow serve                          # start the backend (API + MCP + extension bridge) on :8001
flow serve --host 0.0.0.0           # expose on the network
flow serve --port 8080              # custom port

flow video "a dragon flying over mountains" --aspect landscape --duration 8
flow image "a futuristic neon city" --aspect landscape --count 4
flow edit  "make it anime style" -m MEDIA_ID -v clip.mp4
flow upload clip.mp4                # upload a local asset to Google Flow

flow credits                        # remaining Google Flow credits
flow status                         # is the backend up? is the extension connected?
flow sniff                          # dev: capture Flow API requests
```

`serve` runs the long-lived backend; the other commands either talk to it
(`credits`, `status`) or drive the Chrome extension directly (`video`, `image`,
`edit`, `upload`). Start `flow serve` once and leave it running.

---

## 🌐 OpenAI-Compatible API

While `flow serve` is running, standard endpoints are available at `http://localhost:8001`:

* **`POST /v1/images/generations`** — Generate images.
* **`POST /v1/videos/generations`** — Generate videos.
* **`POST /v1/chat/completions`** — Image/video generation via the chat spec.
* **`GET  /v1/history`** — List generated media files.
* **`GET  /v1/credits`** — Remaining Google Flow credits.
* **`GET  /download/{filename}`** — Download generated files.
* **`GET  /health`** — Backend + extension status.

---

## 🤖 MCP Server

Connect any MCP client — Claude Desktop, Cursor, Cline, Windsurf, Antigravity,
Claude Code, etc. All of them call the same backend, so **`flow serve` must be
running first** (`setup.sh` makes that automatic).

**Full copy-paste config for each client is in [MCP.md](MCP.md).** The short version — a stdio server:

```json
{
  "mcpServers": {
    "flow": {
      "command": "flow-mcp",
      "args": []
    }
  }
}
```

Or, for SSE clients, point at `http://localhost:8001/sse`.

Exposed tools: `get_flow_credits`, `generate_flow_image`, `generate_flow_video`, `upload_flow_media`.

---

## ⚙️ Configuration

All settings live in one file: **`config.env`** (loaded at startup). There are
no secrets, so it's safe to commit. Edit it and restart the backend to apply.

**The knobs you actually change:**

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_PROJECT` | — | Google Flow project ID |
| `IMAGE_MODEL` | `NARWHAL` | Default image model (`lite` / `standard` / `pro`) |
| `SERVER_API_KEY` | _(empty)_ | If set, clients must send `Authorization: Bearer <key>` |
| `MAX_CONCURRENT_REQUESTS` | `5` | Max generations in flight at once (rate limit). |
| `REQUEST_MIN_INTERVAL` | `3` | Min seconds between consecutive generation requests. Prevents Google's `UNUSUAL_ACTIVITY` throttle. |

**Infra defaults you rarely touch:**

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_HOST` / `OPENAI_API_PORT` | `127.0.0.1` / `8001` | Backend bind address |
| `WS_PORT` / `HTTP_PORT` | `9227` / `8100` | Extension bridge ports |
| `POLL_INTERVAL` / `POLL_TIMEOUT` | `10` / `420` | Generation polling |
| `API_REQUEST_TIMEOUT` | `180` | Max seconds for one extension roundtrip. Raise if images time out but still show in Flow. |
| `API_BASE` | `aisandbox-pa.googleapis.com` | Google Flow backend (don't change unless Google moves it) |
