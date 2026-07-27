# 🔌 Connecting the Flow MCP server to your AI client

The Flow backend exposes an **MCP server** so AI clients (Claude Desktop, Cursor,
Cline, Windsurf, Antigravity, Claude Code, etc.) can generate images & video directly.

You add it **yourself** — it's one small config block. Pick your client below.

> **First:** run `./setup.sh` once so the backend auto-starts and the `flow-mcp`
> command exists. The MCP command you point clients at is:
>
> ```
> flow-mcp
> ```
>
> If `flow-mcp` isn't found on PATH, use the full path instead — find it with:
>
> ```bash
> command -v flow-mcp        # e.g. /Users/you/.local/bin/flow-mcp
> ```
>
> The backend (`flow serve`) must be running — `setup.sh` makes that automatic.

---

## Claude Desktop

Edit (create if missing):
`~/Library/Application Support/Claude/claude_desktop_config.json`

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

If you already have other servers, just add the `"flow"` entry inside your
existing `"mcpServers"` object. **Restart Claude Desktop** after saving.

---

## Cursor

`Settings → MCP → Add new MCP server`, or edit `~/.cursor/mcp.json`:

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

---

## Cline (VS Code extension)

Cline → MCP Servers → `Configure MCP Servers`, add:

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

---

## Windsurf

`~/.codeium/windsurf/mcp_config.json`:

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

---

## Google Antigravity

Antigravity → MCP settings → add a server with:

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

---

## Claude Code (CLI)

```bash
claude mcp add flow flow-mcp
```

---

## Any other MCP client

The pattern is always the same — a **stdio** server:

| Field   | Value      |
|---------|------------|
| command | `flow-mcp` |
| args    | *(none)*   |
| type    | stdio      |

If your client only supports **SSE/HTTP**, point it at:

```
http://localhost:8001/sse
```

---

## Tools you get

Once connected, these tools are available to the AI:

- `get_flow_status` — backend, extension, and Flow-key health
- `get_flow_credits` — pooled Google Flow credits
- `list_flow_models` — available models and active default
- `get_flow_history` — recent generated/uploaded media
- `generate_flow_image` — 1-20 images, Nano Banana Pro default, local/media-ID references
- `generate_flow_video` — 1-20 videos, 4/6/8/10 seconds, start image/media ID, references
- `upload_flow_media` — upload a local image/video and return its media ID
- `download_media_from_url` — download an image/video URL locally; optionally upload it to Flow
- `edit_flow_video` — video-to-video editing from a media ID or local video

---

## Troubleshooting

- **Client shows "flow" but tools fail** → the backend isn't running. Check
  `flow status`; if down, `launchctl load ~/Library/LaunchAgents/com.flow.agent.plist`.
- **"command not found: flow-mcp"** → use the full path from `command -v flow-mcp`.
- **Images/videos time out or hit reCAPTCHA limits** → Make sure you are logged in on the Google Flow page. To scale up and bypass rate limits, load the extension across multiple Chrome profiles (multi-browser) or multiple PCs (multi-PC) in your local network. The backend automatically load-balances requests across all active connections.
