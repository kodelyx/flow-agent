# Flow Agent

CLI, OpenAI-compatible API, Chrome extension bridge, and MCP server for Google
Flow image and video generation.

[![Release](https://img.shields.io/github/v/release/kodelyx/flow-agent)](https://github.com/kodelyx/flow-agent/releases)
[![Build](https://github.com/kodelyx/flow-agent/actions/workflows/build.yml/badge.svg)](https://github.com/kodelyx/flow-agent/actions/workflows/build.yml)

Flow Agent uses your existing, logged-in Google Flow browser session. The CLI,
HTTP API, and MCP clients all share one backend and one extension bridge.

## Features

- Text-to-image and reference-image generation
- Text-to-video, image-to-video, first/last-frame, reference-to-video, and video editing
- 4, 6, 8, and 10-second video generation
- 1080p and 4K video delivery through Flow's upsampler, the same pass behind the Flow UI's HD download
- Reusable generated and uploaded media IDs, including after backend restarts
- Exact `--output` paths with real PNG, JPEG, and WebP conversion
- Signature-based MIME and extension detection
- Persistent idempotency for safe paid-generation retries
- OpenAI-compatible HTTP endpoints and pollable video jobs
- MCP v2 tools for generation, upload, download, history, status, and credits
- One shared backend on port `8001`; an already healthy backend is reused

## Requirements

- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/) for source installation
- Chrome or another Chromium browser
- Google Flow access and an active signed-in session

## Quick start

Clone the repository and enter the Python application directory:

```bash
git clone https://github.com/kodelyx/flow-agent.git
cd flow-agent/flow-agent
```

### macOS or Linux setup

The setup script installs the `flow` command and configures the backend to start
on login:

```bash
./scripts/setup.sh
```

To install only the CLI without creating a background service:

```bash
uv tool install --force .
```

### Windows setup

From PowerShell in `flow-agent\flow-agent`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
```

## Chrome extension

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select the repository's [`flow-extension`](flow-extension) directory.
5. Open [Google Flow](https://labs.google/fx/tools/flow), sign in, and keep the tab open.
6. Run `flow status` and confirm both readiness fields are `True`.

Extension-specific details are in
[`flow-extension/README.md`](flow-extension/README.md).

## Backend and readiness

Start the backend manually when it is not managed by the setup service:

```bash
flow
```

Running `flow` again while a healthy Flow backend is already listening on port
`8001` reuses that backend instead of starting a second bridge.

Check readiness with:

```bash
flow status
```

Generation begins only after both of these values are true:

```text
extension_connected: True
has_flow_key:        True
```

## CLI

Run `flow <command> --help` for every available option.

### Images

```bash
flow image "a cinematic neon city" --model narwhal --aspect landscape
flow image "restyle this character" --ref character.png --count 2
flow image "a product photo" --output /absolute/path/result.png
flow image "a product photo" --output /absolute/path/result.jpg
flow image "a product photo" --output /absolute/path/result.webp
```

For `.png`, `.jpg`, `.jpeg`, and `.webp` outputs, the suffix is a format
contract. If Google Flow returns another image format, Flow Agent decodes and
atomically converts it while preserving dimensions. It never writes JPEG bytes
under a `.png` filename.

### Videos

```bash
flow video "a dragon flying over mountains" --aspect landscape --duration 8
flow video "the character starts walking" --start character.png
flow video "transition between scenes" --start first.png --end last.png
flow video "keep this character consistent" --ref character.png
```

#### Resolution

Google Flow generates video at 720p. 1080p and 4K are a second *upsampler* pass
over the finished clip — exactly what the Flow UI does behind its
high-resolution download. Ask for it during generation:

```bash
flow video "a dragon flying over mountains" --resolution 1080p
flow video "a dragon flying over mountains" --resolution 4k -o /absolute/path/dragon.mp4
```

The upsampled file is written to `--output`; the 720p original stays in the
output directory and in `history.json`. If the upsample pass fails, the 720p
video is still delivered and the CLI prints a note.

Upsample a clip you already generated:

```bash
flow upsample GENERATED_VIDEO_MEDIA_ID --resolution 1080p
flow upsample previous_take.mp4 --resolution 4k -o /absolute/path/take_4k.mp4
```

`1080p` upsampling is free. `4k` costs credits and needs a higher Flow tier;
the backend refuses it with HTTP 402 when the balance cannot cover it.

`--start`, `--end`, and `--ref` accept either local image paths or exact media
IDs stored in `history.json`:

```bash
flow video "animate this generated image" --start GENERATED_IMAGE_MEDIA_ID
flow video "use these existing references" --ref GENERATED_ID UPLOADED_ID
```

### Upload and edit

```bash
flow upload character.png
flow upload clips --batch
flow edit "transform into a dark anime style" --media-id VIDEO_MEDIA_ID
```

Uploads return a reusable `media_id`. Generated media IDs are printed after the
saved output path.

### Safe retries

Use one idempotency key when retrying the same paid request:

```bash
flow image "a lighthouse at sunset" --idempotency-key lighthouse-v1
flow video "waves moving slowly" --idempotency-key waves-v1
```

The same key and payload reuse the original result or job. Reusing a key with a
different payload returns a conflict instead of starting another generation.

### Multi-Worker Parallel Batch Generation (`flow batch`)

Flow Agent supports high-speed parallel batch generation across multiple connected Chrome extension workers with **True Round-Robin Load Balancing**:

```bash
# 1. Parallel Batch Images (Default: narwhal / Nano Banana 2, 0 credits):
flow batch prompts.txt -o output_images/ -c 16 -s 1.0 -a landscape

# 2. Parallel Batch Videos (4, 6, 8, 10s durations):
flow batch prompts.txt --type video -d 4 -o output_videos/ -c 4 -s 2.0 -a landscape
```

**Key Batch Capabilities:**
- **Dynamic Multi-Worker Scaling:** Automatically scales concurrency (e.g. 4 workers = 16 parallel slots; 100 workers = 400 slots).
- **True Round-Robin Balancing:** Evenly rotates prompts across all connected browser workers.
- **Smart Tier Priority for Videos:** Automatically drains daily 50 free credits from Freemium workers first before consuming Pro credits.
- **Worker Attribution & Breakdown:** Prints live `via [client_id]` worker logs and displays a final performance breakdown summary table.

**Concurrency & Rate Limiting Configuration:**
- `MAX_CONCURRENT_REQUESTS` (default `4`): Active parallel slots allowed across workers.
- `REQUEST_MIN_INTERVAL` (default `2.0`): Automatic 2.0s stagger gap between consecutive requests to prevent Google Flow server rate limits and dropouts.
- `API_REQUEST_TIMEOUT` (default `60`): Fast timeout with self-healing retry.

## Output files and media history

Without `--output`, files are saved in an `output` directory beside the
installed `flow` executable. Override this location before starting the backend:

```bash
export FLOW_OUTPUT_DIR="$HOME/FlowOutput"
```

An explicit path is resolved to an absolute path and used exactly. Multiple
results keep the first requested path and add `_2`, `_3`, and so on for later
files. The CLI prints the final absolute path and detected format.

`history.json` in the configured output directory is the only persistent media
registry. It stores filename, media ID, media type, MIME type, prompt,
timestamp, project ID, and local/served location metadata when available.

- Generated and uploaded IDs remain reusable after backend restarts.
- Exact media IDs are matched before filename lookup.
- A stale remote ID is re-uploaded only after Google Flow confirms it is unavailable.
- The refreshed ID is written back atomically to the same history record.
- An older `media-id.js` or `media-ids.json` registry is migrated once and removed.
- Missing history and local assets return a clear `Media not found` error.

Flow Agent does not create a second media registry.

## MCP setup

Copy this portable configuration into your MCP client. It intentionally uses
the `flow` command from `PATH` instead of a machine-specific executable path:

```json
{
  "mcpServers": {
    "flow": {
      "command": "flow",
      "args": ["mcp"]
    }
  }
}
```

The backend must be running before MCP tools can generate media. `flow mcp` is
the stdio MCP transport; it does not create a second backend bridge.

If a desktop client cannot find `flow`, locate the installation with:

```bash
command -v flow
```

Use that result in your local client configuration or add its directory to the
client's `PATH`. Do not commit machine-specific executable paths to the
repository.

### Claude Desktop

Create or edit `~/Library/Application Support/Claude/claude_desktop_config.json`
and add the `flow` entry shown above inside `mcpServers`. Restart Claude Desktop
after saving.

### Cursor

Open **Settings → MCP → Add new MCP server**, or add the same configuration to
`~/.cursor/mcp.json`.

### Cline

Open **Cline → MCP Servers → Configure MCP Servers** and add the same `flow`
server configuration.

### Windsurf

Add the same configuration to `~/.codeium/windsurf/mcp_config.json`.

### Google Antigravity

Open Antigravity's MCP settings and add a stdio server with command `flow` and
arguments `mcp`.

### Claude Code

```bash
claude mcp add flow -- flow mcp
```

### SSE/HTTP-only MCP clients

Use the backend's SSE endpoint:

```text
http://127.0.0.1:8001/sse
```

JSON-RPC messages are sent to `http://127.0.0.1:8001/messages`.

### MCP tools

- `get_flow_status` — backend, extension, and Flow-key readiness
- `get_flow_credits` — credits across connected browser sessions
- `list_flow_models` — available models and the active default
- `get_flow_history` — generated and uploaded media history
- `generate_flow_image` — text/reference image generation
- `generate_flow_video` — text, start-image, and reference video generation, with `resolution` for 720p/1080p/4K delivery
- `upsample_flow_video` — upsample an existing video to 1080p or 4K
- `upload_flow_media` — upload a local path, URL, or base64 media payload
- `download_media_from_url` — download media and optionally upload it to Flow
- `edit_flow_video` — edit a video by media ID or local video path

## HTTP API

Default base URL: `http://127.0.0.1:8001`

| Endpoint | Purpose |
|---|---|
| `GET /health` | Backend, extension, and Flow-key health |
| `GET /v1/models` | Available image/video models |
| `GET /v1/credits` | Connected-account credits |
| `GET /v1/history` | Persistent generated/uploaded media history |
| `POST /v1/images/generations` | Generate images |
| `POST /v1/videos/generations` | Submit video generation |
| `GET /v1/videos/generations/{job_id}` | Poll a video or upsample job |
| `POST /v1/videos/upsample` | Upsample an existing video to 1080p or 4K |
| `POST /v1/upload` | Upload an image or video reference |
| `GET /download/{filename}` | Download a managed media file |
| `GET /sse` | MCP over SSE |
| `POST /messages` | MCP over SSE JSON-RPC messages |

`POST /v1/videos/generations` accepts `"resolution": "720p" | "1080p" | "4k"`.
Above 720p the response leads with the upsampled media and still includes the
720p original, each entry carrying `resolution` and, for upsampled files,
`source_media_id`. `POST /v1/videos/upsample` returns the same pollable job
shape and is polled through `GET /v1/videos/generations/{job_id}`.

Send an `Idempotency-Key` header when an HTTP generation request may be
retried. Video submission returns a structured result containing a `job_id`,
status, creation timestamp, and media data. Poll the job endpoint until it is
`succeeded` or `failed`.

If `SERVER_API_KEY` is configured, send it as:

```text
Authorization: Bearer YOUR_SERVER_API_KEY
```

## Configuration

Environment variables take precedence over values in `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_HOST` | `127.0.0.1` | Backend bind/client host |
| `OPENAI_API_PORT` | `8001` | Backend HTTP port |
| `FLOW_OUTPUT_DIR` | beside `flow` | Generated files and `history.json` |
| `FLOW_HISTORY_FILE` | `<output>/history.json` | Optional history path override |
| `DEFAULT_PROJECT` | bundled default | Google Flow project ID |
| `IMAGE_MODEL` | `narwhal` | Default image model (Nano Banana 2) |
| `PUBLIC_BASE_URL` | `http://localhost:8001` | URLs returned for local media |
| `SERVER_API_KEY` | unset | Optional backend bearer authentication |
| `FLOW_READY_TIMEOUT` | `30` | CLI readiness wait in seconds |
| `FLOW_VIDEO_POLL_TIMEOUT` | `900` | CLI video-job polling timeout |
| `MAX_CONCURRENT_REQUESTS` | `5` | Maximum concurrent Flow requests |
| `REQUEST_MIN_INTERVAL` | `3` | Minimum seconds between request starts |
| `VIDEO_UPSAMPLER_1080P_MODEL` | `veo_3_1_upsampler_1080p` | Flow 1080p upsampler model key |
| `VIDEO_UPSAMPLER_4K_MODEL` | `veo_3_1_upsampler_4k` | Flow 4K upsampler model key |
| `VIDEO_UPSAMPLE_ENUM_1080P` | `VIDEO_RESOLUTION_1080P` | 1080p resolution enum sent to Flow |
| `VIDEO_UPSAMPLE_ENUM_4K` | `VIDEO_RESOLUTION_4K` | 4K resolution enum sent to Flow |

The four upsampler variables exist because Flow's upsample API is undocumented.
If Google renames a model key or a resolution enum, override it here instead of
patching code. A rejected enum is retried with the known spelling variants and
finally without the field at all, so the model key alone can carry the target.

Image model aliases:

- `harbor_seal` / `lite`
- `narwhal` / `standard`
- `gem_pix_2` / `pro`

## Exit codes

The unified CLI uses stable non-zero codes for actionable failures:

| Code | Meaning |
|---|---|
| `2` | Invalid CLI arguments |
| `3` | Backend unavailable |
| `4` | Extension or Flow key not ready |
| `5` | Backend/API generation error |
| `6` | Media conversion, download, or output-writing error |

## Development

Install dependencies and run the regression suite:

```bash
cd flow-agent/flow-agent
uv sync --extra test
uv run pytest -q
uvx ruff check . --select F,E9
```

Build a standalone executable with the same Python environment used by the
project:

```bash
uv run --with pyinstaller python -m PyInstaller --clean --noconfirm flow.spec
```

The generated binary is written to `dist/flow` (`dist/flow.exe` on Windows).
`build/` and `dist/` are ignored build artifacts and should not be committed.

## Troubleshooting

- **`flow` is not found** — reinstall from `flow-agent/flow-agent` with
  `uv tool install --force .`, then ensure the directory printed by
  `command -v flow` is available to your shell or MCP client.
- **Backend reports `extension_connected=False`** — open Google Flow in Chrome,
  enable the extension, and refresh the Flow tab.
- **Backend reports `has_flow_key=False`** — keep the signed-in Flow tab open
  and trigger a refresh so the extension can capture the current key.
- **Port 8001 is already in use** — `flow` reuses a healthy Flow backend. Stop
  or reconfigure an unrelated service occupying that port.
- **`Media not found`** — regenerate or upload the asset if neither its history
  record nor local managed file still exists.
- **A retry might have reached Flow** — repeat it with the same idempotency key;
  do not create a new key for the same paid request.
- **1080p or 4K came back as 720p** — read the `note` in the response. Flow
  rejected or failed the upsample pass; the 720p original is still delivered.
  If Flow changed the wire format, override `VIDEO_UPSAMPLER_*_MODEL` or
  `VIDEO_UPSAMPLE_ENUM_*` and retry. `python -m flow_server.sniff` captures the
  Flow UI's own upsample request for comparison.
- **MCP tools appear but fail** — verify `flow status`, then restart the MCP
  client after correcting its command/PATH configuration.

## Project layout

```text
flow-agent/
├── README.md             # All installation, CLI, API, and MCP documentation
├── flow-agent/           # Python CLI, backend, and MCP implementation
│   ├── main.py           # Unified `flow` entry point
│   ├── flow.spec         # Single-executable PyInstaller build
│   ├── flow_server/      # API, MCP/SSE, media registry, and server state
│   ├── flow_engine/      # Flow bridge, generators, polling, and uploads
│   ├── scripts/          # Setup and uninstall scripts
│   └── tests/            # Regression and acceptance tests
├── flow-extension/       # Chrome extension bridge
└── .github/workflows/    # Cross-platform test/build workflow
```

## License

Use Google Flow and generated media according to Google's applicable terms.
