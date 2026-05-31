# ⚡ Omni Flash

**Generate & edit AI videos using Google's Gemini Omni model.**

Upload videos, convert to anime, generate from text or image — all automated. No API key needed.

## ✅ Tested & Working

| Feature | Time | Command |
|---------|------|---------|
| **T2V** (Text→Video) | ~44s | `python omni.py "A samurai on a cliff at sunset"` |
| **V2V** (Video→Video edit) | ~3min | `python edit_full.py "Make it anime" -m MEDIA_ID -v video.mp4` |
| **I2V** (Image→Video) | ~44s | Via `upload_image()` + `generate_video_i2v()` in `omni.py` |
| **Video Upload** | ~12s | `python upload.py video.mp4` |
| **Image Upload** | ~10s | Via `upload_image()` in `omni.py` |

## 📋 Requirements

- **Python 3.9+** with `websockets` (`pip install websockets`)
- **Chrome** with the extension installed (see below)
- A **Google account** logged into [labs.google/fx/tools/flow](https://labs.google/fx/tools/flow)

## 🚀 Quick Start

### 1. Install the Chrome Extension

1. Open Chrome → `chrome://extensions`
2. Enable "Developer mode"
3. Click "Load unpacked" → select the `extension/` folder
4. Open [labs.google/fx/tools/flow](https://labs.google/fx/tools/flow) in a tab

### 2. Text → Video (T2V)

```bash
# Portrait (9:16)
python omni.py "A dragon breathing fire in a dark cave"

# Landscape (16:9)
python omni.py "Eagle soaring over mountains" --aspect landscape

# Custom output
python omni.py "Dog dancing" -o dog_dance.mp4
```

### 3. Upload Video → Edit Style (V2V)

```bash
# Step 1: Upload video (auto-saves media_id to media-id.js)
python upload.py my_video.mp4

# Step 2: Edit with style prompt
python edit_full.py "Transform into vibrant anime style" \
  --media-id <MEDIA_ID_FROM_STEP_1> \
  --video-file my_video.mp4 \
  -o output/anime/ --merge
```

### 4. Image → Video (I2V)

```python
from omni import ExtensionBridge, upload_image, generate_video_i2v, poll_status, download_video, ASPECTS, DEFAULT_PROJECT

bridge = ExtensionBridge()
await bridge.start()
await bridge.wait_for_extension(30)

# Upload image → get media_id (auto-saved to media-id.js)
img_id = await upload_image(bridge, "my_image.png")

# Generate video from image
mids = await generate_video_i2v(bridge, "The scene comes alive with motion",
    ASPECTS['portrait'], DEFAULT_PROJECT, img_id, duration=8)

# Poll + download
await poll_status(bridge, mids[0], DEFAULT_PROJECT)
await download_video(bridge, mids[0], "output.mp4")
```

### 5. Batch Upload

```bash
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
├── omni.py            # Core: ExtensionBridge, T2V, I2V, V2V, polling, download
├── upload.py          # Upload video to Flow → get media_id
├── edit_full.py       # Full video editor (split → edit → merge)
├── upload_chunks.py   # Batch upload all video chunks
├── sniff.py           # API request sniffer (discover new endpoints)
├── models.json        # Model keys & API config
├── media-id.js        # Auto-updated: filename → media_id mapping
├── SNIFFING.md        # API discovery guide
├── extension/         # Chrome extension
│   ├── manifest.json
│   ├── background.js  # WS client, API proxy, upload handler
│   ├── content.js     # Bridge between background & injected
│   └── injected.js    # Fetch interceptor, reCAPTCHA solver
└── README.md
```

## 🎯 Models & Endpoints

| Model | Key | Type |
|-------|-----|------|
| Omni Flash T2V 4s | `abra_t2v_4s` | Text → Video |
| Omni Flash T2V 6s | `abra_t2v_6s` | Text → Video |
| Omni Flash T2V 8s | `abra_t2v_8s` | Text → Video |
| Omni Flash T2V 10s | `abra_t2v_10s` | Text → Video |
| Omni Flash Edit | `abra_edit` | Video → Video |

| Endpoint | URL |
|----------|-----|
| T2V | `/v1/video:batchAsyncGenerateVideoText` |
| I2V | `/v1/video:batchAsyncGenerateVideoStartImage` |
| V2V Edit | `/v1/video:batchAsyncGenerateVideoEditVideo` |
| Upload Image | `/v1/flow/uploadImage` |
| Poll Status | `/v1/video:batchCheckAsyncVideoGenerationStatus` |

## 📝 CLI Reference

### omni.py — Text to Video
```
python omni.py "prompt" [-o output.mp4] [-a portrait|landscape]
```

### upload.py — Upload Video
```
python upload.py video.mp4 [--project-id ID]
```

### edit_full.py — Video Editor (V2V)
```
python edit_full.py "edit prompt" -m MEDIA_ID [-v video.mp4] [-o output/] [--merge]
```

### sniff.py — API Sniffer
```
python sniff.py
```

## ⚠️ Notes

- Requires an active Google Flow session in Chrome
- Uses your Google account's Flow credits
- Extension must be connected (green badge in Chrome toolbar)
- `media-id.js` auto-updates on every upload (video or image)
- Video upload uses Google's resumable GCS protocol

## License

MIT
