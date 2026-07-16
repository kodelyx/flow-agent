# ⚡ Flow Agent

A programmable, **OpenAI-compatible** image & video generation API + CLI on top of **Google Flow (Google Labs)** — plus an **MCP server** so Claude (and other MCP clients) can generate media directly.

It works by bridging to the **Flow Chrome extension** over WebSocket, executing commands inside a logged-in Google Flow browser session.

> **Requirement:** Chrome with the Flow Agent extension installed and logged in at `labs.google/fx/tools/flow`. The extension is the generation engine — the backend just drives it. (No Docker required.)

---

## ⚡ Easy Install (Windows / macOS / Linux)

The easiest way to run Flow Agent is by downloading the pre-built standalone binaries. No Python installation required!

1. Go to the **[Releases](https://github.com/kodelyx/flow-agent/releases/latest)** page.
2. Download the files for your Operating System:
   - **Windows:** Download `flow-cli-windows.exe` and `flow-mcp-windows.exe`.
   - **macOS:** Download `flow-cli-macos` and `flow-mcp-macos`.
   - **Linux:** Download `flow-cli-linux` and `flow-mcp-linux`.
3. Open a terminal or command prompt in the folder where you downloaded them, and you can run them directly!

*Note: If you need to change settings (like your Google Flow project ID), just download `config.env`, put it next to your binaries, and edit it.*

---

## 🛠️ Developer Install (Python / uv)

If you prefer to install it from source or use it as a standard Python tool:

### Using `uv` (Recommended)
```bash
uv tool install git+https://github.com/kodelyx/flow-agent
```

### Auto-start Setup (macOS / Linux / Windows)
You can configure the backend to start automatically on every login.

#### macOS & Linux
In the `flow-agent/` directory, run:
```bash
./setup.sh
```
This will set up a LaunchAgent (macOS) or a systemd user service (Linux) to auto-start and keep the backend alive. To disable:
```bash
./uninstall.sh
```

#### Windows
Open PowerShell in the `flow-agent/` directory and run:
```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
.\setup-windows.ps1
```
This creates a startup shortcut in your Windows Startup folder. To disable:
```powershell
.\uninstall-windows.ps1
```

---

## 🚀 CLI Usage

Whether you downloaded the binary (e.g., `flow-cli-windows.exe`) or installed it via Python (e.g., `flow`), the commands are the same:

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

`serve` runs the long-lived backend; the other commands either talk to it (`credits`, `status`) or drive the Chrome extension directly (`video`, `image`, `edit`, `upload`). Start `flow serve` once and leave it running.

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

Connect any MCP client — Claude Desktop, Cursor, Cline, Windsurf, Antigravity, Claude Code, etc. All of them call the same backend, so **the backend (`flow serve`) must be running first**.

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
*(Note: If you downloaded the pre-built binaries, replace `"flow-mcp"` with the absolute path to your downloaded `flow-mcp` file, e.g., `"C:\\Downloads\\flow-mcp-windows.exe"`).*

Exposed tools: `get_flow_credits`, `generate_flow_image`, `generate_flow_video`, `upload_flow_media`.

---

## ⚙️ Configuration

All settings live in **`config.env`**. The binaries have defaults built-in, but to override them, create a `config.env` file in the same folder as your executable.

**The knobs you actually change:**

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_PROJECT` | — | Google Flow project ID |
| `IMAGE_MODEL` | `NARWHAL` | Default image model (`lite` / `standard` / `pro`) |
| `SERVER_API_KEY` | _(empty)_ | If set, clients must send `Authorization: Bearer <key>` |
| `MAX_CONCURRENT_REQUESTS` | `5` | Max generations in flight at once (rate limit). |
