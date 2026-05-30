# 🔍 API Sniffing Guide

How to discover new Google Flow API endpoints using the Chrome extension's built-in request sniffer.

## How It Works

```
Flow UI (browser)
    ↓ user clicks "Generate" / "Upload" etc.
    ↓
fetch() call to aisandbox-pa.googleapis.com
    ↓ intercepted by injected.js (monkey-patched fetch)
    ↓
postMessage → content.js → background.js
    ↓
HTTP POST → http://127.0.0.1:8100/api/ext/callback
    ↓
Your sniff server logs the URL + payload
```

The extension's `injected.js` monkey-patches `window.fetch` to intercept ALL outgoing requests to `aisandbox-pa.googleapis.com`. Every request's URL, method, and body are forwarded to your local server.

## Quick Start

### 1. Start the Sniff Server

```python
python sniff.py
```

This starts:
- WebSocket server on `ws://127.0.0.1:9222` (extension connects here)
- HTTP server on `http://127.0.0.1:8100` (receives sniffed data)

### 2. Open Flow UI

Go to [labs.google/fx/tools/flow](https://labs.google/fx/tools/flow) in Chrome.
The extension will auto-connect to your sniff server.

### 3. Perform the Action

Do whatever you want to discover the API for:
- Upload an image/video
- Generate a video
- Change settings
- Click any button

### 4. Read the Logs

The sniff server prints every intercepted request:
```
🔍 SNIFFED: https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText
   Method: POST
   Payload: {"mediaGenerationContext":{"batchId":"..."},"clientContext":{...},"requests":[...]}
```

## Sniff Server Code

Save this as `sniff.py`:

```python
#!/usr/bin/env python3
"""Sniff server — captures all Flow UI API requests."""

import asyncio, json, websockets, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('sniff')

# Store all sniffed requests
all_requests = []

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if body.get('type') == 'sniffed_video_request':
            url = body.get('url', '')
            method = body.get('method', '?')
            payload = body.get('payload', '')

            log.info('🔍 %s %s', method, url)
            if payload:
                log.info('   %s', str(payload)[:1000])

            all_requests.append({
                'url': url,
                'method': method,
                'payload': payload,
                'timestamp': body.get('timestamp'),
            })

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def log_message(self, *a): pass

# Start HTTP server
srv = HTTPServer(('127.0.0.1', 8100), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
log.info('HTTP callback on :8100')

async def on_connect(ws):
    log.info('✅ Extension connected!')
    async for raw in ws:
        data = json.loads(raw)
        if data.get('type') == 'token_captured':
            log.info('🔑 Token captured')

async def main():
    async with websockets.serve(on_connect, '127.0.0.1', 9222):
        log.info('⚡ WS server on :9222')
        log.info('👉 Open Flow UI and perform any action...')
        await asyncio.Future()

asyncio.run(main())
```

## What Gets Captured

| Action in Flow UI | API Endpoint |
|-------------------|-------------|
| Generate video (T2V) | `/v1/video:batchAsyncGenerateVideoText` |
| Generate video (I2V) | `/v1/video:batchAsyncGenerateVideoStartImage` |
| Generate video (Edit) | `/v1/video:batchAsyncGenerateVideoEditVideo` |
| Poll video status | `/v1/video:batchCheckAsyncVideoGenerationStatus` |
| Upload image | `/v1/flow/uploadImage` |
| Generate image | `/v1/projects/{id}/flowMedia:batchGenerateImages` |
| Get credits | `/v1/credits` |
| Get media | `/v1/media/{media_id}` |
| Upscale video | `/v1/video:batchAsyncGenerateVideoUpsampleVideo` |

## How to Find New Endpoints

### Example: Finding Video Upload

1. Start `sniff.py`
2. Open Flow UI
3. Drag & drop a video file into Flow
4. Check logs — you'll see the upload URL and payload format
5. Add the new endpoint to `models.json`

### Example: Finding Model Keys

1. Start `sniff.py`  
2. Open Flow UI
3. Select different model (e.g., Omni Flash 4s)
4. Click Generate
5. Check logs — look for `videoModelKey` in the payload

```json
"requests": [{
    "videoModelKey": "abra_t2v_4s",   ← this is what you need
    ...
}]
```

## Architecture

```
┌─────────────────────────────────────────────┐
│  injected.js (MAIN world)                   │
│  - Monkey-patches window.fetch              │
│  - Captures URL + body of every request     │
│  - Posts to content.js via postMessage       │
└──────────────────┬──────────────────────────┘
                   │ postMessage
┌──────────────────▼──────────────────────────┐
│  content.js (ISOLATED world)                │
│  - Listens for __FLOWKIT_SNIFF__ messages    │
│  - Forwards to background.js                │
└──────────────────┬──────────────────────────┘
                   │ chrome.runtime.sendMessage
┌──────────────────▼──────────────────────────┐
│  background.js (Service Worker)             │
│  - Receives SNIFFED_AISANDBOX_REQUEST       │
│  - POSTs to http://127.0.0.1:8100/callback  │
└──────────────────┬──────────────────────────┘
                   │ HTTP POST
┌──────────────────▼──────────────────────────┐
│  sniff.py (Your server)                     │
│  - Logs every request                       │
│  - Saves URL + method + payload             │
└─────────────────────────────────────────────┘
```

## Tips

- **Filter by keyword**: Modify the sniff server to only log URLs containing specific words (e.g., `upload`, `video`, `generate`)
- **Save to file**: Add `json.dump(all_requests, open('sniffed.json', 'w'))` to save all captured requests
- **Compare payloads**: Run the same action with different settings and diff the payloads to find which fields control what
- **Telemetry noise**: Ignore URLs containing `batchLog`, `fetchUserRecommendations`, `frontendEvents` — these are analytics, not API calls
