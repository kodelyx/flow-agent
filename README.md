<div align="center">

# ⚡ Flow Agent

**Generate AI images and videos from Google Flow — via CLI, REST API, or your AI assistant.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-REST%20Server-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![MCP](https://img.shields.io/badge/MCP-Claude%20%7C%20Cursor%20%7C%20AGY-7c3aed?style=flat-square)](https://modelcontextprotocol.io/)
[![Tests](https://img.shields.io/badge/Tests-77%20Passing-22c55e?style=flat-square&logo=pytest&logoColor=white)](flow-agent/tests/)
[![License](https://img.shields.io/badge/License-MIT-f59e0b?style=flat-square)](LICENSE)

<br/>

```
"a glowing crystal lotus on calm water"  →  🖼️  output/acct-38e6.jpg  (26 s)
"hyper-lapse sunrise over ocean horizon"  →  🎬  output/acct-video.mp4  (58 s)
```

</div>

---

## What does it do?

Flow Agent is a **local server and CLI** that connects to Google Flow to generate high-quality AI images and videos. You set it up once, and then you can use it three ways:

| How | Best For |
| :--- | :--- |
| 🖥️ **Terminal (CLI)** | Quick one-off generations, scripting |
| 🌐 **REST API** | Connecting any app — works exactly like OpenAI's API |
| 🤖 **MCP Server** | Letting Claude, Cursor, or AGY generate for you automatically |

---

## Features at a Glance

- 🖼️ **Text → Image** — aspect ratios: `1:1` `16:9` `9:16` `4:3` `3:4`, up to 4 variations at once
- 🎬 **Text → Video** — durations: `4s` `6s` `8s` `10s`, quality: `360p` / `720p`
- 🎞️ **Image → Video** — animate any image into a video clip
- 👥 **Multi-Account** — add multiple Google accounts to pool credits
- 🔌 **OpenAI Compatible** — drop-in replacement: change `base_url`, nothing else
- 📊 **History & Stats** — every generation logged to local SQLite with credits tracking
- 🩺 **Self-Diagnostics** — `./bin/flow doctor` tells you exactly what to fix

---

## Quick Start

### 1 — Install

```bash
cd flow-agent

python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -e '.[test]'
```

### 2 — Connect Your Google Account

> You need the **Flow Chrome Extension** installed in Chrome first.  
> Load it from the `flow-extension/` folder via `chrome://extensions → Load unpacked`.

```bash
# Start the WebSocket bridge (keep this running in a terminal)
./bin/flow bridge
```

Then open **[https://labs.google/fx/tools/flow](https://labs.google/fx/tools/flow)** in Chrome while signed in to your Google account. The extension will automatically save your session to `cookies/`.

### 3 — Verify Setup

```bash
./bin/flow doctor
```

You should see:
```
ok    version     flow-go 0.1.0
ok    database    data/flow.db
ok    cookies     1 account(s) loaded
ok    browser     extension attached
```

### 4 — Generate Your First Image

```bash
python main.py image "a glowing crystal lotus on calm water" --aspect 1:1
```

```
✓ saved output/acct-38e60406-67b.jpg  (26 s)
```

---

## Usage

### 🖼️ Images

```bash
# Basic — saves to output/
python main.py image "PROMPT"

# With options
python main.py image "PROMPT" \
  --aspect 16:9          # 1:1 | 16:9 | 9:16 | 4:3 | 3:4 | square | landscape | portrait
  --count 2              # 1–4 variations
  --model narwhal        # narwhal | harbor_seal | gem_pix_2

# Run on all accounts at once (pools credits)
python main.py image "PROMPT" --all
```

### 🎬 Videos

```bash
# Text to video
python main.py video "PROMPT" \
  --aspect landscape     # landscape | portrait | 16:9 | 9:16
  --duration 8s          # 4s | 6s | 8s | 10s
  --quality 720p         # 360p | 720p

# Image to video (animate a photo)
python main.py video "camera slowly pans right" \
  --start-image output/my-image.jpg
```

### 💰 Balance & Stats

```bash
python main.py balance            # cached (instant)
python main.py balance --refresh  # live probe from Google Flow

python main.py stats              # generation history + credit usage
python main.py projects           # list your Flow projects
```

---

## REST API Server

```bash
python main.py server --port 8001
```

Interactive docs open at **`http://127.0.0.1:8001/docs`**

### All Endpoints

| Method | Path | Description |
| :--- | :--- | :--- |
| `POST` | `/api/v1/image` | Generate image |
| `POST` | `/api/v1/video` | Generate video |
| `GET` | `/api/v1/balance` | Credit balance |
| `GET` | `/api/v1/stats` | Generation history |
| `GET` | `/api/v1/projects` | List Flow projects |
| `GET` | `/api/v1/media/{file}` | Download result |
| `GET` | `/health` | Server liveness |
| `POST` | `/v1/images/generations` | OpenAI DALL-E compat |
| `POST` | `/v1/chat/completions` | OpenAI Chat compat + SSE |

> Every `/api/v1/*` route also has a `/v1/*` alias — use whichever you prefer.

### Quick API Example

```bash
curl -X POST http://127.0.0.1:8001/api/v1/image \
  -H "Content-Type: application/json" \
  -d '{"prompt": "neon cyber dragon over misty mountains", "aspect": "16:9"}'
```

```json
{
  "job_id": "ba884266-...",
  "status": "succeeded",
  "elapsed_seconds": 26.3,
  "files": [
    { "url": "http://127.0.0.1:8001/api/v1/media/acct-38e60406-67b.jpg" }
  ]
}
```

---

## OpenAI SDK — Drop-in Replacement

Change **one line** in your existing OpenAI code:

```python
from openai import OpenAI

# ← Change only this line
client = OpenAI(base_url="http://127.0.0.1:8001/v1", api_key="not-needed")

# Everything else stays exactly the same ↓
response = client.images.generate(
    prompt="a cute red origami fox sitting in autumn leaves",
    size="1792x1024",   # → Flow 16:9
    n=1
)
print(response.data[0].url)
```

Supported OpenAI sizes → Flow mapping:

| `size` | Flow aspect |
| :--- | :--- |
| `1024x1024` / `square` | `1:1` |
| `1792x1024` / `landscape` | `16:9` |
| `1024x1792` / `portrait` | `9:16` |
| `1365x1024` | `4:3` |
| `1024x1365` | `3:4` |

---

## MCP — Use With Claude / Cursor / AGY

Flow Agent exposes 5 tools via the [Model Context Protocol](https://modelcontextprotocol.io/), so your AI assistant can generate images and videos directly for you.

### Setup (Claude Desktop)

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "flow": {
      "command": "python3",
      "args": ["/absolute/path/to/flow-agent/main.py", "mcp"]
    }
  }
}
```

### Setup (Cursor / AGY)

```json
{
  "flow": {
    "command": "python3",
    "args": ["/absolute/path/to/flow-agent/main.py", "mcp"]
  }
}
```

### Available MCP Tools

| Tool | What it does |
| :--- | :--- |
| `flow_generate_image` | Generate image from prompt |
| `flow_generate_video` | Generate video from prompt or image |
| `flow_get_balance` | Check credit balance |
| `flow_get_stats` | View generation history |
| `flow_get_projects` | List Flow projects |

Now just ask your assistant: *"Generate a 16:9 image of a cyberpunk city at dawn using Flow"*

---

## Project Structure

```
flow-agent/
│
├── bin/
│   ├── flow              → auto-selects the right binary
│   ├── flow-macos        → macOS universal binary (arm64 + x86_64)
│   ├── flow-linux        → Linux binary
│   └── flow-windows.exe  → Windows binary
│
├── cookies/
│   └── account_<id>.json → saved Google account sessions
│
├── data/
│   └── flow.db           → SQLite: generation history + credits
│
├── output/
│   └── *.jpg, *.mp4      → your generated images and videos
│
├── flow_agent/
│   ├── api.py            → FastAPI REST server (Native + OpenAI routes)
│   ├── engine.py         → subprocess bridge to bin/flow
│   └── mcp_server.py     → MCP JSON-RPC server
│
├── docs/                 → detailed reference docs
├── tests/                → 77 passing tests
├── main.py               → CLI entrypoint
└── pyproject.toml
```

---

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `IMAGE_MODEL` | `narwhal` | Default image model (`narwhal` / `harbor_seal` / `gem_pix_2`) |
| `FLOW_COOKIES_DIR` | `cookies/` | Directory with account JSON files |
| `FLOW_OUTPUT_DIR` | `output/` | Where generated files are saved |
| `FLOW_DB_PATH` | `data/flow.db` | SQLite history database path |

---

## Troubleshooting

| Problem | Fix |
| :--- | :--- |
| `400 Bad Request` / unusual activity | Browser tab at `labs.google/fx/tools/flow` must be **open and in foreground** with extension active |
| `0 projects` in logs | The account's `project_id` field is empty — open Flow in Chrome to auto-populate it |
| `no extension attached` | Install the extension, run `./bin/flow bridge`, open Flow tab in Chrome |
| Two accounts conflicting | Close all extra Chrome profiles — only keep one Flow tab open at a time |
| `pytest` fails with `PermissionError` | Run with `--basetemp=/tmp/fa_test` to use a writable temp directory |

---

## Run Tests

```bash
python3 -m pytest -q --basetemp=/tmp/fa_test
```

```
77 passed in 0.38s
```

---

## Documentation

| Doc | Description |
| :--- | :--- |
| 📘 [API Reference](docs/API_REFERENCE.md) | Every endpoint, parameter, and response schema |
| ⌨️ [CLI Reference](docs/CLI_REFERENCE.md) | All commands, flags, env vars, exit codes |
| 🤖 [MCP Guide](docs/MCP_GUIDE.md) | Claude, Cursor, Zed, AGY setup |
| 🏗️ [Architecture](docs/ARCHITECTURE.md) | How the engine, bridge, and server fit together |
| 🔌 [Extension Bridge](docs/EXTENSION_BRIDGE.md) | Chrome extension + WebSocket protocol |

---

## License

MIT — free to use, modify, and distribute.
