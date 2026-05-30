# ⚡ Omni Flash

**Generate AI videos in ~45 seconds using Google's Gemini Omni model.**

Single-file Python script. No API key needed. No server to run. Just one command.

## 🎬 Demo

```bash
python omni.py "A powerful lion roaring on a cliff at sunset, epic cinematic 4K"
```

```
⚡ WebSocket server on ws://127.0.0.1:9222
✅ Extension connected!
🔑 Auth token captured
🎬 Generating: "A powerful lion roaring..." [abra_t2v_10s]
✅ Submitted! media_id=a6e6668b, credits=204
⏳ Waiting... (0s → 11s → 22s → 34s)
✅ Video ready! (45s)
✅ Saved: lion.mp4 (2.7 MB)
🎉 Done!
```

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

### 2. Generate Videos

```bash
# Portrait (9:16) — default
python omni.py "A dragon breathing fire in a dark cave"

# Landscape (16:9)
python omni.py "Eagle soaring over mountains" --aspect landscape

# Custom output path
python omni.py "Dog dancing on stage" -o dog_dance.mp4
```

## ⚙️ How It Works

```
omni.py (WS server + HTTP callback)
    ↕ WebSocket (:9222)
Chrome Extension (auth token + reCAPTCHA)
    ↕ HTTPS
Google Omni API (aisandbox)
```

1. `omni.py` starts a WebSocket server and HTTP callback server
2. Chrome extension connects automatically and provides authentication
3. Script sends video generation request through the extension
4. Extension solves reCAPTCHA and proxies the API call with browser cookies
5. Script polls for completion, then downloads the MP4

## 📁 Project Structure

```
omni-flash/
├── omni.py          # Main script (standalone, ~280 lines)
├── extension/       # Chrome extension (FlowKit bridge)
│   ├── manifest.json
│   ├── background.js
│   ├── content.js
│   ├── injected.js
│   └── popup/
└── README.md
```

## 🎯 Model Details

| Parameter | Value |
|-----------|-------|
| Model | `abra_t2v_10s` (Gemini Omni Flash) |
| Type | Text-to-Video (T2V) |
| Duration | 10 seconds |
| Speed | ~30-45 seconds generation |
| Aspects | Portrait (9:16), Landscape (16:9) |

## 📝 CLI Options

```
usage: omni.py [-h] [--output OUTPUT] [--aspect {portrait,landscape}] [--project-id PROJECT_ID] prompt

positional arguments:
  prompt                Text prompt for video

options:
  -h, --help            Show help
  --output, -o          Output file (default: omni_output.mp4)
  --aspect, -a          Aspect ratio: portrait or landscape
  --project-id, -p      Google Flow project ID
```

## ⚠️ Notes

- Requires an active Google Flow session in Chrome
- Uses your Google account's Flow credits
- Extension must be connected (green badge ● in Chrome toolbar)
- One video = ~15 credits

## License

MIT
