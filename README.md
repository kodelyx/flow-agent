# ⚡ Omni Flash

**Automate Google's Gemini Omni video generation model — Text→Video, Image→Video, Video→Video editing — all from the terminal.**

No API key needed. Uses your Google account's free Flow credits via a Chrome extension bridge.

---

## ✅ Features & Status

| Feature | What it does | Time | Status |
|---------|-------------|------|--------|
| **T2V** | Generate video from text prompt | ~44s | ✅ Working |
| **V2V** | Edit/restyle existing video | ~3min | ✅ Working |
| **I2V** | Animate a still image into video | ~44s | ✅ Working |
| **Upload** | Upload video/image to Flow | ~12s | ✅ Working |
| **Batch Upload** | Upload entire folder of videos | varies | ✅ Working |
| **API Sniffer** | Discover new endpoints/payloads | - | ✅ Working |

---

## 📋 Prerequisites

| Requirement | Details |
|-------------|---------|
| **Python** | 3.9 or higher |
| **Chrome** | Latest version |
| **Google Account** | Logged into Flow |
| **ffmpeg** | Only for V2V merge (optional) |

---

## 🛠️ Installation (Step by Step)

### Step 1: Clone the repo

```bash
git clone https://github.com/kodelyx/omni-flash.git
cd omni-flash
```

### Step 2: Install Python dependencies

```bash
pip install -r requirements.txt
```

This installs `websockets` (the only dependency).

### Step 3: Install the Chrome Extension

1. Open Chrome browser
2. Go to `chrome://extensions` in the address bar
3. Toggle **"Developer mode"** ON (top-right corner)
4. Click **"Load unpacked"**
5. Select the `extension/` folder from this repo
6. You should see the FlowKit extension appear

### Step 4: Open Google Flow

1. Open [labs.google/fx/tools/flow](https://labs.google/fx/tools/flow) in Chrome
2. Make sure you're **logged into your Google account**
3. The extension icon should show a **green badge** = connected
4. **Keep this tab open** while using Omni Flash

> ⚠️ The Flow tab MUST stay open. The extension captures auth tokens from it.

---

## 🚀 Usage

### Text → Video (T2V)

Generate a new video from a text description.

```bash
# Basic (portrait 9:16, 10 seconds)
python -m cli.generate "A samurai drawing his katana on a cliff at golden sunset"

# Landscape mode (16:9)
python -m cli.generate "Eagle soaring over snowy mountains" --aspect landscape

# Custom output file
python -m cli.generate "A dragon breathing fire" -o dragon.mp4

# Shorter duration (4/6/8/10 seconds)
python -m cli.generate "Dog playing in the park" --duration 6

# Generate multiple variations
python -m cli.generate "Cyberpunk city at night" --count 4
```

**CLI Options:**
| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--output` | `-o` | `omni_output.mp4` | Output filename |
| `--aspect` | `-a` | `portrait` | `portrait` or `landscape` |
| `--duration` | `-d` | `10` | `4`, `6`, `8`, or `10` seconds |
| `--count` | `-c` | `1` | Generate 1-4 videos |
| `--edit` | `-e` | - | Pass media_id for V2V edit mode |

---

### Upload Video

Upload a local video to Google Flow. Returns a `media_id` needed for V2V editing.

```bash
# Single video
python -m cli.upload my_video.mp4

# Batch upload all .mp4 in a folder
python -m cli.upload chunks/ --batch
```

The `media_id` is **automatically saved** to `media-id.js`:
```
my_video.mp4 : 49f7d936-01e3-41ad-917a-2f9bb6ead00b
```

---

### Video → Video Edit (V2V)

Apply style changes to an uploaded video (e.g., convert to anime).

```bash
# Step 1: Upload your video
python -m cli.upload my_video.mp4
# → media_id saved to media-id.js

# Step 2: Edit with style prompt
python -m cli.edit "Transform into vibrant anime style, Studio Ghibli aesthetic" \
    --media-id 49f7d936-01e3-41ad-917a-2f9bb6ead00b \
    --video-file my_video.mp4 \
    --output output_anime/ \
    --merge

# Without local video file (specify duration manually)
python -m cli.edit "Make it look cyberpunk neon" \
    -m MEDIA_ID \
    --total-seconds 30 \
    -o output_cyber/
```

**How it works:** Long videos are automatically split into 10s segments, each processed in parallel, then merged with ffmpeg.

**CLI Options:**
| Flag | Short | Required | Description |
|------|-------|----------|-------------|
| `--media-id` | `-m` | Yes | Flow media ID (from upload) |
| `--video-file` | `-v` | No | Local file (auto-detects duration/fps) |
| `--total-seconds` | `-t` | No | Duration if no local file |
| `--output` | `-o` | No | Output directory (default: `output/`) |
| `--aspect` | `-a` | No | `portrait` or `landscape` |
| `--merge` | | No | Merge segments with ffmpeg |

---

### Image → Video (I2V)

Animate a still image into a video. Requires Python scripting (no CLI yet):

```python
import asyncio
from omniflash import (
    ExtensionBridge, upload_image, generate_video_i2v,
    poll_status, download_video, ASPECTS, DEFAULT_PROJECT,
)

async def main():
    # Connect to extension
    bridge = ExtensionBridge()
    await bridge.start()
    await bridge.wait_for_extension(30)

    # Upload your image
    img_id = await upload_image(bridge, "my_image.png")
    print(f"Image uploaded: {img_id}")

    # Generate video from image
    media_ids = await generate_video_i2v(
        bridge,
        prompt="The character comes alive, dramatic movement, cinematic",
        aspect=ASPECTS['portrait'],       # or ASPECTS['landscape']
        project_id=DEFAULT_PROJECT,
        image_media_id=img_id,
        duration=8,                        # 4, 6, 8, or 10 seconds
    )

    # Wait for video to finish
    if media_ids:
        await poll_status(bridge, media_ids[0], DEFAULT_PROJECT)
        await download_video(bridge, media_ids[0], "output_i2v.mp4")

    await bridge.close()

asyncio.run(main())
```

---

### API Sniffer

Capture all API requests made by the Flow UI. Useful for discovering new endpoints or debugging.

```bash
# Start sniffer, then use Flow UI normally
python -m cli.sniff

# Save captured requests to file
python -m cli.sniff --save sniffed.json
```

See [SNIFFING.md](SNIFFING.md) for the full API discovery guide.

---

## 🐍 Python API (for developers)

Use the `omniflash` package directly in your own scripts:

```python
from omniflash import (
    ExtensionBridge,          # WebSocket bridge to Chrome extension
    generate_video,           # T2V: text → video
    edit_video,               # V2V: video → video
    upload_image,             # Upload image → get media_id
    generate_video_i2v,       # I2V: image → video
    poll_status,              # Poll until video is ready
    download_video,           # Download finished video
    ASPECTS,                  # {'portrait': '...', 'landscape': '...'}
    DEFAULT_PROJECT,          # Your default project ID
)
from omniflash.upload import upload_video  # Upload video file
from omniflash import media_store          # Read/write media-id.js

# media_store examples:
media_store.save("video.mp4", "uuid-here")   # Save entry
mid = media_store.get("video.mp4")            # Get media_id
all_entries = media_store.read_entries()       # Get all entries
```

> **Backward compatible:** `from omni import ExtensionBridge, generate_video, ...` still works.

---

## 📁 Project Structure

```
omni-flash/
├── omniflash/                  # Core Python package
│   ├── __init__.py             # Public API exports
│   ├── bridge.py               # ExtensionBridge (WS + HTTP server)
│   ├── config.py               # Config loader (models.json)
│   ├── media_store.py          # media-id.js read/write
│   ├── upload.py               # Video upload (GCS resumable)
│   └── generators/             # API functions
│       ├── common.py           # poll_status, download_video
│       ├── t2v.py              # Text → Video
│       ├── v2v.py              # Video → Video (edit)
│       └── i2v.py              # Image → Video + upload_image
├── cli/                        # CLI entry points
│   ├── generate.py             # python -m cli.generate
│   ├── upload.py               # python -m cli.upload
│   ├── edit.py                 # python -m cli.edit
│   └── sniff.py                # python -m cli.sniff
├── extension/                  # Chrome extension
│   ├── manifest.json           # Extension manifest
│   ├── background.js           # WS client, API proxy
│   ├── content.js              # Page ↔ background bridge
│   └── injected.js             # Fetch interceptor, reCAPTCHA
├── omni.py                     # Backward-compatible wrapper
├── models.json                 # API config & model keys
├── media-id.js                 # Auto-updated filename → media_id
├── requirements.txt            # Python dependencies
├── SNIFFING.md                 # API discovery guide
└── README.md
```

---

## ⚙️ How It Works

```
┌─────────────────────────────────┐
│  Your Terminal / Python Script  │
│  python -m cli.generate "..."   │
└──────────┬──────────────────────┘
           │ import omniflash
           ▼
┌─────────────────────────────────┐
│  omniflash package              │
│  ExtensionBridge (WS + HTTP)    │
└──────────┬──────────────────────┘
           │ WebSocket (:9222)
           │ HTTP callback (:8100)
           ▼
┌─────────────────────────────────┐
│  Chrome Extension (FlowKit)     │
│  Auth token + reCAPTCHA solving │
└──────────┬──────────────────────┘
           │ HTTPS (browser cookies)
           ▼
┌─────────────────────────────────┐
│  Google Omni API (aisandbox)    │
│  Video generation / editing     │
└─────────────────────────────────┘
```

1. Python starts a WebSocket server + HTTP callback server
2. Chrome extension auto-connects and provides authentication
3. Script sends API requests through the extension
4. Extension solves reCAPTCHA and proxies with browser cookies
5. Script polls for completion, then downloads the result

---

## 🎯 Models & Endpoints

| Model | Key | Duration | Type |
|-------|-----|----------|------|
| Omni Flash T2V 4s | `abra_t2v_4s` | 4 sec | Text → Video |
| Omni Flash T2V 6s | `abra_t2v_6s` | 6 sec | Text → Video |
| Omni Flash T2V 8s | `abra_t2v_8s` | 8 sec | Text → Video |
| Omni Flash T2V 10s | `abra_t2v_10s` | 10 sec | Text → Video |
| Omni Flash Edit | `abra_edit` | 10 sec | Video → Video |

| Endpoint | Path |
|----------|------|
| T2V | `/v1/video:batchAsyncGenerateVideoText` |
| I2V | `/v1/video:batchAsyncGenerateVideoStartImage` |
| V2V Edit | `/v1/video:batchAsyncGenerateVideoEditVideo` |
| Upload Image | `/v1/flow/uploadImage` |
| Poll Status | `/v1/video:batchCheckAsyncVideoGenerationStatus` |
| Get Media | `/v1/video/media/{media_id}` |

---

## 🔧 Troubleshooting

| Problem | Solution |
|---------|----------|
| Extension not connecting | Make sure Flow tab is open and you're logged in |
| `Address already in use` | Another script is using port 9222/8100. Kill it first |
| `TIMEOUT` error | Extension may have disconnected. Reload Flow tab |
| `reCAPTCHA failed` | Reload Flow tab, wait a few seconds, try again |
| `No media in response` | Check your prompt. Some prompts get blocked |
| `curl failed` | Upload too large or network issue. Retry |
| Video quality poor | Use longer duration (10s) and detailed prompts |
| V2V merge fails | Install ffmpeg: `brew install ffmpeg` |

---

## ⚠️ Important Notes

- **Flow tab must stay open** in Chrome while using Omni Flash
- Uses your Google account's **free Flow credits** (check remaining in Flow UI)
- Extension auto-reconnects if you reload the Flow tab
- `media-id.js` auto-updates on every upload (video or image)
- All generated videos save to the `output/` directory by default
- Old `from omni import ...` syntax still works (backward compatible)

---

## 📄 License

MIT
