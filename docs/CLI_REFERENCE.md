# Flow Agent — CLI Reference

Complete command-line documentation for **Flow Agent** and the underlying **Flow Engine** binary.

- **Python CLI**: `python main.py <command>` or `python -m flow_agent <command>`
- **Engine Binary**: `./bin/flow` (auto-selects `flow-macos` / `flow-linux` / `flow-windows.exe` by platform)

---

## Table of Contents

1. [Python CLI (`main.py`)](#1-python-cli-mainpy)
   - [server](#11-server--fastapi-http-server)
   - [mcp](#12-mcp--model-context-protocol-server)
   - [image](#13-image--text-to-image-generation)
   - [video](#14-video--textimage-to-video-generation)
   - [balance](#15-balance--account-credits)
   - [stats](#16-stats--generation-history)
   - [projects](#17-projects--list-flow-projects)
2. [Engine Binary (`./bin/flow`)](#2-engine-binary-binflow)
3. [Environment Variables](#3-environment-variables)
4. [Exit Codes](#4-exit-codes)

---

## 1. Python CLI (`main.py`)

```
usage: flow-agent [-h] {server,mcp,image,video,balance,stats,projects} ...
```

---

### 1.1 `server` — FastAPI HTTP Server

Starts the REST API server with both Native (`/api/v1/*`) and OpenAI-compatible (`/v1/*`) endpoints.

```bash
python main.py server [--host HOST] [--port PORT] [--log-level LEVEL]
```

| Flag | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--host` | string | `127.0.0.1` | Bind address |
| `--port` | integer | `8001` | Port number |
| `--log-level` | string | `info` | Logging level: `debug`, `info`, `warning`, `error` |

**Example:**
```bash
# Start on all interfaces, port 9000
python main.py server --host 0.0.0.0 --port 9000 --log-level debug
```

---

### 1.2 `mcp` — Model Context Protocol Server

Starts the MCP JSON-RPC server for AI assistants (Claude, Cursor, Antigravity IDE).

```bash
# Stdio transport (default — for desktop MCP clients)
python main.py mcp

# SSE transport (for web/remote MCP clients)
python main.py mcp --sse [--host HOST] [--port PORT]
```

| Flag | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--sse` | flag | off | Enable SSE transport over HTTP |
| `--host` | string | `127.0.0.1` | Bind address (SSE only) |
| `--port` | integer | `8002` | Port (SSE only) |

**MCP Tools exposed:**
- `flow_generate_image` — text-to-image
- `flow_generate_video` — text/image-to-video
- `flow_get_balance` — account credits
- `flow_get_stats` — generation history
- `flow_get_projects` — list Flow projects

---

### 1.3 `image` — Text-to-Image Generation

Generates one or more images from a text prompt directly in the terminal.

```bash
python main.py image PROMPT [OPTIONS]
```

| Argument / Flag | Type | Default | Choices | Description |
| :--- | :--- | :--- | :--- | :--- |
| `prompt` | string | — | — | Scene description **(required)** |
| `--aspect` | string | `1:1` | `1:1` `16:9` `9:16` `4:3` `3:4` `square` `landscape` `portrait` | Image aspect ratio |
| `--count` | integer | `1` | `1` `2` `3` `4` | Number of image variations |
| `--model` | string | `narwhal` | — | Model key (see models table below) |
| `--all` | flag | off | — | Run on every account in `cookies/` concurrently |
| `--cookies` | string | `null` | — | Target a specific account file, e.g. `cookies/account_abc.json` |

**Available Models:**

| Model Key | Description |
| :--- | :--- |
| `narwhal` | Default Flow image model — best quality |
| `harbor_seal` | Alternative image model |
| `gem_pix_2` | Gemini Pixel model variant |

> Set `$IMAGE_MODEL` to override the default model without passing `--model` every time.

**Examples:**

```bash
# Basic 16:9 image
python main.py image "a glowing neon cyber dragon soaring over mountains" --aspect 16:9

# 4 portrait variations with a specific model
python main.py image "origami hummingbird on cherry blossom" \
  --aspect 9:16 --count 4 --model harbor_seal

# Run across all accounts simultaneously
python main.py image "cyberpunk city at dawn" --all

# Use a specific account cookie file
python main.py image "sunset over ocean" --cookies cookies/account_abc.json
```

> **Validation**: `--aspect`, `--count`, and `--model` are validated before any network call. Invalid values print a usage error and exit with code `2`.

---

### 1.4 `video` — Text/Image-to-Video Generation

Generates a video clip from a text description, or animates a starting image into video.

```bash
python main.py video PROMPT [OPTIONS]
```

| Argument / Flag | Type | Default | Choices | Description |
| :--- | :--- | :--- | :--- | :--- |
| `prompt` | string | — | — | Video description **(required)** |
| `--aspect` | string | `landscape` | `landscape` `portrait` `16:9` `9:16` | Video orientation |
| `--duration` | string | `8s` | `4s` `6s` `8s` `10s` | Clip length |
| `--quality` | string | `720p` | `360p` `720p` | Output resolution |
| `--count` | integer | `1` | `1` `2` `3` `4` | Number of variations |
| `--start-image` | string | `null` | — | Local image path or media ID (enables image-to-video) |
| `--all` | flag | off | — | Run on every account concurrently |
| `--cookies` | string | `null` | — | Target a specific account file |

**Examples:**

```bash
# 8-second landscape video
python main.py video "hyper-lapse sunrise over ocean horizon" --duration 8s

# Short vertical reel
python main.py video "neon rain on city streets" --aspect portrait --duration 4s --quality 720p

# Animate an image into video (image-to-video)
python main.py video "camera slowly panning right" --start-image output/my_image.jpg

# Animate a previously uploaded media asset
python main.py video "zoom into the galaxy" --start-image 4cb43f55-2897-47cd-b2bf-117f2e5f450c
```

> **Validation**: `--aspect`, `--duration`, and `--quality` are strictly validated. Unsupported values (e.g. `--duration 5s`, `--quality 1080p`) exit with code `2` and a descriptive error — they are **not silently coerced**.

---

### 1.5 `balance` — Account Credits

Shows available generation credits across all configured accounts.

```bash
python main.py balance [--refresh]
```

| Flag | Description |
| :--- | :--- |
| `--refresh` | Probe upstream Google Flow servers for live balance (adds ~5–8 s). Without this flag, reads instantly from the local SQLite cache. |

**Examples:**

```bash
# Instant cached read
python main.py balance

# Live upstream sync
python main.py balance --refresh
```

**Sample output:**
```
acct-e50d5ecec52e   1049 cr   active
acct-1a102fc1d7a1     50 cr   active
─────────────────────────────────────
Total                1099 cr
```

---

### 1.6 `stats` — Generation History

Shows lifetime generation history, status breakdown, and credit consumption from `data/flow.db`.

```bash
python main.py stats
```

**Sample output:**
```
29 generations recorded
  succeeded: 14  |  empty: 15
  image: 22  |  video: 7
  media files: 14
  credits spent: 0
```

---

### 1.7 `projects` — List Flow Projects

Lists Google Flow projects associated with the active account.

```bash
python main.py projects [--cookies COOKIES_FILE]
```

| Flag | Description |
| :--- | :--- |
| `--cookies` | Use a specific account file instead of the auto-selected default |

**Example:**
```bash
python main.py projects --cookies cookies/account_abc.json
```

---

## 2. Engine Binary (`./bin/flow`)

The low-level Go binary (`flow-macos` on macOS, `flow-linux` on Linux, `flow-windows.exe` on Windows) is invoked automatically by the Python layer. It can also be used directly for advanced workflows.

### 2.1 Doctor — Environment Self-Test

```bash
./bin/flow doctor
```

Runs end-to-end checks on the engine, database, cookies, and browser extension. Fix every `warn` or `error` before running generations.

**Sample output:**
```
ok    version     flow-go 0.1.0
ok    database    data/flow.db (14 rows)
ok    cookies     1 account(s) loaded
warn  browser     no extension attached (captcha will fall back to http)
```

### 2.2 Cookies — Inspect Cookie Validity

```bash
./bin/flow cookies
```

Lists all loaded account files, their account IDs, and project IDs without making any network requests.

### 2.3 Balance — Live Credit Probe

```bash
./bin/flow balance [--json]
```

Probes upstream Flow servers for current credit balance. `--json` emits machine-readable JSON.

### 2.4 Stats — SQLite History

```bash
./bin/flow stats
```

Dumps the raw generation log from `data/flow.db`.

### 2.5 Projects — List Flow Projects

```bash
./bin/flow projects [--json]
```

Lists projects for the active account. `--json` returns a bare JSON array.

### 2.6 Image Generation (Shorthand)

```bash
./bin/flow image PROMPT ASPECT [COUNT]
```

```bash
# Single image, square
./bin/flow image "neon origami owl" 1:1

# Two 16:9 images
./bin/flow image "neon origami owl" 16:9 x2

# With explicit captcha mode (required when browser tab is open)
./bin/flow image "neon origami owl" 16:9 --captcha broker
```

### 2.7 Video Generation (Shorthand)

```bash
./bin/flow generate PROMPT ASPECT DURATION QUALITY [COUNT]
```

```bash
./bin/flow generate "cyberpunk street in rain" 9:16 8s 720p x1
```

### 2.8 Upload Video

```bash
./bin/flow upload-video FILENAME
```

Uploads a local video file to Google Flow project storage. Returns a reusable `media_id` for use as `--start-image` in video generation.

```bash
./bin/flow upload-video scene.mp4
# → media_id: "4cb43f55-2897-47cd-b2bf-117f2e5f450c"
```

### 2.9 Bridge — Chrome Extension WebSocket

```bash
./bin/flow bridge
```

Starts the WebSocket bridge on `ws://127.0.0.1:9222` that the Chrome Flow extension connects to. Used for high-trust reCAPTCHA token minting when running headlessly. Normally you do **not** need to call this directly — it starts automatically during generation when `--captcha broker` is set.

### 2.10 Version

```bash
./bin/flow version
# → flow-go 0.1.0
```

---

## 3. Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `IMAGE_MODEL` | `narwhal` | Default image model key. Overrides the `--model` default without passing the flag. |
| `FLOW_COOKIES_DIR` | `cookies/` | Directory to scan for account JSON bundles. |
| `FLOW_OUTPUT_DIR` | `output/` | Directory where generated images and videos are saved. |
| `FLOW_DB_PATH` | `data/flow.db` | SQLite database path for generation history. |

---

## 4. Exit Codes

| Code | Meaning |
| :--- | :--- |
| `0` | Success |
| `1` | Generation failed (upstream error, network error, etc.) |
| `2` | Invalid CLI argument (argparse validation failure — invalid choices, missing required arguments, etc.) |
