# ⚡ Omni Flash

**Generate & edit AI videos using Google's Gemini Omni model.**

Upload videos, convert to anime, generate from text — all automated. No API key needed.

## 🎬 What It Does

| Feature | Command |
|---------|---------|
| **Text → Video** | `python omni.py "A dragon breathing fire"` |
| **Video → Video** (style edit) | `python edit_full.py "Make it anime" -m MEDIA_ID -v video.mp4` |
| **Upload Video** | `python upload.py chunks/chunk_00.mp4` |
| **Batch Upload** | `python upload_chunks.py chunks/` |
| **API Sniffing** | `python sniff.py` |

## 📋 Requirements

- **Python 3.9+** with `websockets` (`pip install websockets`)
- **Chrome** with the [FlowKit extension](extension/) installed
- A **Google account** logged into [labs.google/fx/tools/flow](https://labs.google/fx/tools/flow)

## 🚀 Quick Start

### 1. Install the Chrome Extension

1. Open Chrome → `chrome://extensions`
2. Enable "Developer mode"
3. Click "Load unpacked" → select the `extension/` folder
4. Open [labs.google/fx/tools/flow](https://labs.google/fx/tools/flow) in a tab

### 2. Text → Video

```bash
# Portrait (9:16) — default
python omni.py "A dragon breathing fire in a dark cave"

# Landscape (16:9)
python omni.py "Eagle soaring over mountains" --aspect landscape
```

### 3. Upload & Edit Video

```bash
# Upload a video → auto saves media_id to media-id.js
python upload.py chunks/chunk_00.mp4

# Convert to anime style
python edit_full.py "Make it anime style, Studio Ghibli aesthetic" \
  --media-id 6b8a77a4-b8b0-4f52-913e-50a222b147b4 \
  --video-file chunks/chunk_00.mp4 \
  -o output/anime/ --merge
```

### 4. Batch Upload

```bash
# Upload all chunks at once
python upload_chunks.py chunks/
```

## ⚙️ How It Works

```
Python scripts (WS server + HTTP callback)
    ↕ WebSocket (:9222)
Chrome Extension (auth token + reCAPTCHA)
    ↕ HTTPS
Google Omni API (aisandbox)
```

1. Python starts a WebSocket server and HTTP callback
2. Chrome extension connects and provides authentication
3. Script sends requests through the extension
4. Extension solves reCAPTCHA and proxies API calls with browser cookies
5. Script polls for completion, then downloads the result

## 📁 Project Structure

```
omni-flash/
├── omni.py            # Core: ExtensionBridge, T2V, V2V, polling, download
├── upload.py          # Upload video to Flow → get media_id
├── edit_full.py       # Full video editor (split → edit → merge)
├── upload_chunks.py   # Batch upload all video chunks
├── sniff.py           # API request sniffer (discover new endpoints)
├── models.json        # Model keys & API config
├── media-id.js        # Auto-updated: filename → media_id mapping
├── SNIFFING.md        # API discovery guide
├── extension/         # Chrome extension (FlowKit bridge)
│   ├── manifest.json
│   ├── background.js  # WS client, API proxy, upload handler
│   ├── content.js     # Bridge between background & injected
│   └── injected.js    # Fetch interceptor, reCAPTCHA solver
└── README.md
```

## 🎯 Models

| Model | Key | Type | Duration |
|-------|-----|------|----------|
| Omni Flash T2V | `abra_t2v_4s/6s/8s/10s` | Text → Video | 4-10s |
| Omni Flash Edit | `abra_edit` | Video → Video | 10s segments |

## 📝 CLI Reference

### omni.py — Text to Video
```
python omni.py "prompt" [-o output.mp4] [-a portrait|landscape]
```

### upload.py — Upload Video
```
python upload.py video.mp4 [--project-id ID]
```
Auto-updates `media-id.js` with the filename and media_id.

### edit_full.py — Video Editor
```
python edit_full.py "edit prompt" -m MEDIA_ID -v video.mp4 [-o output/] [--merge]
```
Splits video into 10s segments, edits each with the prompt, downloads results.

### sniff.py — API Sniffer
```
python sniff.py
```
Captures all Flow UI API requests for endpoint discovery.

## ⚠️ Notes

- Requires an active Google Flow session in Chrome
- Uses your Google account's Flow credits
- Extension must be connected (green badge in Chrome toolbar)
- Video upload uses Google's resumable upload protocol (X-Goog-Upload-Command headers)

## License

MIT
