# ⚡ Flow Agent

CLI, OpenAI-compatible API, Chrome extension, and MCP server for Google Flow image/video generation.

[![Release](https://img.shields.io/github/v/release/kodelyx/flow-agent)](https://github.com/kodelyx/flow-agent/releases)
[![Build](https://github.com/kodelyx/flow-agent/actions/workflows/build.yml/badge.svg)](https://github.com/kodelyx/flow-agent/actions/workflows/build.yml)

## Features

- Text-to-image and reference-based image generation
- Text-to-video, image-to-video, reference-to-video, and video editing
- 4/6/8/10-second video generation
- OpenAI-compatible HTTP API
- CLI for full control
- MCP v2 with status, credits, models, history, generation, upload, URL download, and video editing
- Chrome extension bridge using your logged-in Google Flow session
- `gem_pix_2` (Pro) as the default image model

## Install from source

Requirements: Python 3.10+, Chrome, Google Flow access, and the `uv` package manager.

```bash
git clone https://github.com/kodelyx/flow-agent.git
cd flow-agent
cp config.env.example config.env
uv tool install --force .
```

Start the backend:

```bash
flow serve
```

## Chrome extension

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select the [`flow-extension`](flow-extension) folder.
5. Open <https://labs.google/fx/tools/flow>, sign in, and keep the page open.
6. Verify with `flow status`.

Extension-specific instructions: [flow-extension/README.md](flow-extension/README.md).

## CLI

```bash
flow status
flow credits

flow image "a cinematic neon city" --model gem_pix_2 --aspect landscape
flow image "restyle this character" --ref character.png --count 2

flow video "a dragon flying over mountains" --aspect landscape --duration 8
flow video "the character starts walking" --start character.png
flow video "transition between scenes" --start first.png --end last.png

flow upload clip.mp4
flow edit "transform into a dark anime style" --media-id MEDIA_ID --video-file clip.mp4
```

Run `flow <command> --help` for every option.

## MCP v2

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

Available tools:

- `get_flow_status`
- `get_flow_credits`
- `list_flow_models`
- `get_flow_history`
- `generate_flow_image`
- `generate_flow_video`
- `upload_flow_media`
- `download_media_from_url`
- `edit_flow_video`

`download_media_from_url` accepts direct or signed image/video links, saves them locally, and can optionally upload them to Flow.

More client configurations: [MCP.md](MCP.md).

## HTTP API

Default base URL: `http://127.0.0.1:8001`

| Endpoint | Purpose |
|---|---|
| `GET /health` | Backend and extension health |
| `GET /v1/models` | Available models |
| `GET /v1/credits` | Connected-account credits |
| `GET /v1/history` | Generated media history |
| `POST /v1/images/generations` | Generate images |
| `POST /v1/videos/generations` | Generate videos |
| `POST /v1/upload` | Upload image/video references |

## Default model

```env
IMAGE_MODEL=gem_pix_2
```

Available aliases:

- `harbor_seal` / `lite`
- `narwhal` / `standard`
- `gem_pix_2` / `pro`

## Project layout

```text
flow-agent/
├── cli/                 # API and direct generation commands
├── flow_cli/            # Unified CLI entry point
├── omniflash/           # Flow bridge and generators
├── flow-extension/      # Chrome extension
├── flow_mcp_server.py   # MCP v2 server
├── config.env.example
├── flow-cli.spec
└── flow-mcp.spec
```

## Security

- Never commit browser cookies, GitHub tokens, tunnel credentials, or generated private media.
- The repository ignores local `config.env`, `github-token`, `cloudflared/`, outputs, logs, and build artifacts.
- Set `SERVER_API_KEY` before exposing the API outside localhost.

## License

Use Google Flow and generated media according to Google's applicable terms.
