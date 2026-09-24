# Flow Agent CLI Reference

Comprehensive command-line interface documentation for **Flow Agent** (`main.py`) and the underlying **Flow Engine** (`./bin/flow`).

---

## 1. Unified Python CLI (`main.py`)

Run via `python main.py <command>` (or `python -m flow_agent <command>`):

```bash
usage: flow-agent [-h] {server,mcp,image,video,balance,stats,projects} ...
```

### 1.1. `server` (FastAPI Server)
Runs the HTTP API server providing Native and OpenAI-compatible endpoints.

```bash
python main.py server [--host 127.0.0.1] [--port 8001] [--log-level info]
```
- **`--host`** (string): Bind address (default: `127.0.0.1`).
- **`--port`** (integer): Port number (default: `8001`).
- **`--log-level`** (string): Logging level: `debug`, `info`, `warning`, `error` (default: `info`).

---

### 1.2. `mcp` (Model Context Protocol Server)
Starts the MCP JSON-RPC server for AI assistants (Claude, Cursor, Antigravity).

```bash
# Stdio transport (default, for desktop clients):
python main.py mcp

# SSE transport (for web clients):
python main.py mcp --sse [--host 127.0.0.1] [--port 8002]
```
- **`--sse`**: Enable Server-Sent Events over HTTP.
- **`--port`**: Port for SSE server (default: `8002`).

---

### 1.3. `image` (Text-to-Image Generation)
Generate an image directly from the terminal.

```bash
python main.py image "a glowing neon cyber dragon soaring over mountains" \
  --aspect 16:9 \
  --count 1 \
  --model narwhal
```
- **`prompt`** (positional, required): Scene prompt.
- **`--aspect`**: Aspect ratio: `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `square`, `landscape`, `portrait` (default: `1:1`).
- **`--count`**: Variations count: `1` to `4` (default: `1`).
- **`--model`**: Model name: `narwhal`, `harbor_seal` (default: `narwhal`).
- **`--all`**: Concurrently generate on every account in `cookies/`.
- **`--cookies`**: Target a specific account file (e.g., `cookies/account_c13eea595e47.json`).

---

### 1.4. `video` (Text/Image-to-Video Generation)
Generate or animate video clips.

```bash
python main.py video "hyper-lapse sunrise over ocean horizon" \
  --aspect landscape \
  --duration 8s \
  --quality 720p
```
- **`prompt`** (positional, required): Video description.
- **`--aspect`**: `landscape`, `portrait`, `16:9`, `9:16` (default: `landscape`).
- **`--duration`**: Video length: `4s`, `6s`, `8s`, `10s` (default: `8s`).
- **`--quality`**: Resolution: `360p`, `720p` (default: `720p`).
- **`--start-image`**: Path to local image file or media ID for image-to-video animation.

---

### 1.5. `balance` (Account Credits)
Shows available credits across all signed-in accounts.

```bash
# Cached read from SQLite (instant, offline):
python main.py balance

# Live upstream probe:
python main.py balance --refresh
```

---

### 1.6. `stats` (Analytics & History)
Shows lifetime generations, status breakdown, and credit consumption.

```bash
python main.py stats
```

---

### 1.7. `projects` (Flow Projects)
Lists remote Flow projects stored under the active account.

```bash
python main.py projects [--cookies cookies/account_c13eea595e47.json]
```

---

## 2. Low-Level Engine CLI (`./bin/flow`)

The compiled Go engine binary provides direct administrative subcommands:

### 2.1. Bridge Server
Listens for the Flow Chrome extension on WebSocket `ws://127.0.0.1:9222`:

```bash
./bin/flow bridge
```

### 2.2. Shorthand Generation Syntax
The binary supports rapid positional shorthand tokens:

```bash
# Image generation: prompt + aspect + count
./bin/flow image "neon origami owl" 1:1 x2

# Video generation: prompt + aspect + duration + quality + count
./bin/flow generate "cyberpunk street in rain" 9:16 8s 720p x1
```

### 2.3. Video Editing & Uploading
Upload a local video asset to obtain a reusable Flow media ID:

```bash
./bin/flow upload-video scene.mp4
# Returns media ID: "12345-abcdef"

# Edit the uploaded video:
./bin/flow edit scene.mp4 "add glowing neon lightning in the sky"
```

### 2.4. Cookie Diagnostics
Inspect local cookie validity without network requests:

```bash
./bin/flow cookies
```

### 2.5. Environment Doctor
Run end-to-end self-tests on the engine, database, and accounts:

```bash
./bin/flow doctor
```
