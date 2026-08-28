#!/usr/bin/env python3
import sys
import json
import base64
import os
import urllib.request
import urllib.error
import tempfile
import shutil
import uuid
from urllib.parse import urlparse, unquote

from flow_server.media_types import extension_for_media, extension_for_mime, sniff_media_type
from flow_server.media_history import record_local_media
from flow_engine.config import OUTPUT_DIR

# Load .env so MCP sees the same user settings as the backend. Legacy
# config.env remains supported for existing installations.
def _load_env_files():
    if getattr(sys, 'frozen', False):
        root = os.path.dirname(sys.executable)
    else:
        root = os.path.dirname(os.path.abspath(__file__))
    for filename in (".env", "config.env"):
        path = os.path.join(root, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip().strip("'\""))
        except Exception:
            pass

_load_env_files()

# Flow Agent Backend Base URL — honour the same env vars the backend binds to,
# so MCP keeps working even if the port is changed in .env.
_API_HOST = os.environ.get("OPENAI_API_HOST", "127.0.0.1")
_API_PORT = os.environ.get("OPENAI_API_PORT", "8001")
FLOW_API_URL = os.environ.get("FLOW_API_URL", f"http://{_API_HOST}:{_API_PORT}")
DEFAULT_IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gem_pix_2").lower()
_MODEL_ALIASES = {
    "lite": "harbor_seal",
    "harbor_seal": "harbor_seal",
    "standard": "narwhal",
    "narwhal": "narwhal",
    "pro": "gem_pix_2",
    "nano_banana_2": "gem_pix_2",
    "gem_pix_2": "gem_pix_2",
}

def _normalise_model(model):
    return _MODEL_ALIASES.get(str(model or DEFAULT_IMAGE_MODEL).strip().lower(), str(model or DEFAULT_IMAGE_MODEL).strip().lower())

def _request_json(path, payload=None, method=None, timeout=30):
    url = f"{FLOW_API_URL}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method or ("POST" if data is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
        return json.loads(body.decode("utf-8")) if body else {}

def _file_data_uri(path):
    if not path or not os.path.isfile(path):
        raise ValueError(f"File does not exist: {path}")
    mime = sniff_media_type(path)
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{encoded}"

def _safe_filename(name):
    name = os.path.basename(unquote(name or "")).strip().replace("\x00", "")
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "._- ").strip(" .")
    return cleaned[:180] or "downloaded_media"

def _extension_for_mime(mime):
    return extension_for_mime(mime)

def ensure_backend_running():
    try:
        req = urllib.request.Request(f"{FLOW_API_URL}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass

    log_debug("Backend not running. Auto-starting backend server internally...")
    try:
        import subprocess, time
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, "-m", "main"]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(15):
            time.sleep(1)
            try:
                with urllib.request.urlopen(f"{FLOW_API_URL}/health", timeout=1) as resp:
                    if resp.status == 200:
                        log_debug("Backend server auto-started successfully!")
                        return True
            except Exception:
                pass
    except Exception as e:
        log_debug(f"Auto-start backend failed: {e}")
    return False

def log_debug(msg):
    # MCP uses stdout for protocol communication, so all debug logs MUST go to stderr
    sys.stderr.write(f"[Flow MCP] {msg}\n")
    sys.stderr.flush()

def _download_bytes(url, timeout=60, attempts=3):
    """Fetch a URL's bytes, bypassing any HTTP proxy (Google's signed GCS URLs
    403 through proxies) and retrying transient failures. Returns bytes or None."""
    import time
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=timeout) as resp:
                if getattr(resp, "status", 200) == 200:
                    body = resp.read()
                    if body:
                        return body
                    last_err = "empty body"
                else:
                    last_err = f"status {resp.status}"
        except Exception as e:
            last_err = str(e)
        log_debug(f"download attempt {attempt}/{attempts} failed: {last_err}")
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    log_debug(f"download failed after {attempts} attempts: {last_err}")
    return None

def handle_initialize(request_id, params=None):
    ensure_backend_running()
    # Echo the client's protocol version when provided (falls back to a known
    # good one) so newer MCP clients don't warn about a version mismatch.
    client_ver = (params or {}).get("protocolVersion") or "2024-11-05"
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": client_ver,
            "capabilities": {
                "tools": {}
            },
            "serverInfo": {
                "name": "flow-agent",
                "version": "2.0.7"
            }
        }
    }
    return response

def handle_tools_list(request_id):
    tools = [
        {
            "name": "get_flow_status",
            "description": "Check Flow backend, Chrome extension connection, and Flow session-key health.",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "get_flow_credits",
            "description": "Check the remaining credits / generations on the logged-in Google Flow account.",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        },
        {
            "name": "list_flow_models",
            "description": "List image models available through the Flow backend.",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "get_flow_history",
            "description": "List recently generated or uploaded Flow media.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
                }
            }
        },
        {
            "name": "generate_flow_image",
            "description": "Generate 1-20 images using Google Flow, with model selection and local/media-ID references.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Text description of the image to generate"
                    },
                    "size": {
                        "type": "string",
                        "description": "Dimensions of the output image (default: '1280x720')",
                        "default": "1280x720"
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of image variations (1-20)",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 1
                    },
                    "ref_image_path": {
                        "type": "string",
                        "description": "Optional local file path to a reference image on the host for Image-to-Image"
                    },
                    "ref_image_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": "Optional local reference-image paths (up to 10)"
                    },
                    "ref_media_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": "Optional already-uploaded Flow media IDs"
                    },
                    "model": {
                        "type": "string",
                        "description": "Image model to use (harbor_seal/lite, narwhal/standard, gem_pix_2/pro)",
                        "default": "gem_pix_2"
                    },
                    "seed": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4294967295,
                        "description": "Explicit seed. Reuse it to reproduce a look; omit for a fresh random image. Multi-image requests offset from this value."
                    }
                },
                "required": ["prompt"]
            }
        },
        {
            "name": "generate_flow_video",
            "description": "Generate 1-20 Flow videos with duration, aspect, start asset, seed, first-last frame, reference-media, and delivery-resolution control (720p native, 1080p or 4K via Flow's upsampler).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Description of the motion to generate in the video"
                    },
                    "aspect": {
                        "type": "string",
                        "description": "Video aspect ratio: 'landscape' or 'portrait' (default: 'landscape')",
                        "enum": ["landscape", "portrait"],
                        "default": "landscape"
                    },
                    "start_image_path": {
                        "type": "string",
                        "description": "Optional local file path to a starting reference image on the host for Image-to-Video"
                    },
                    "duration": {"type": "integer", "enum": [4, 6, 8, 10], "default": 8},
                    "count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1},
                    "start_media_id": {"type": "string", "description": "Optional pre-uploaded start image media ID"},
                    "ref_media_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": "Optional Flow reference-media IDs for reference-to-video"
                    },
                    "seed": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4294967295,
                        "description": "Explicit seed. Reuse it to re-roll a shot while holding its look; omit for a fresh random take. Multi-take requests offset from this value."
                    },
                    "end_image_path": {
                        "type": "string",
                        "description": "Optional local end-frame image. With a start image this switches to first-last frame mode: the clip morphs from start to end."
                    },
                    "end_media_id": {
                        "type": "string",
                        "description": "Optional pre-uploaded end-frame media ID; same first-last frame mode as end_image_path"
                    },
                    "video_model": {
                        "type": "string",
                        "description": "Override the Flow videoModelKey (defaults to abra_t2v_<duration>s)"
                    },
                    "resolution": {
                        "type": "string",
                        "enum": ["720p", "1080p", "4k"],
                        "default": "720p",
                        "description": "Delivery resolution. Flow generates at 720p; '1080p' (free) or '4k' (paid, higher tier) add Flow's upsampler pass and the high-resolution file is returned first."
                    }
                },
                "required": ["prompt"]
            }
        },
        {
            "name": "upsample_flow_video",
            "description": "Upsample an existing Flow video to 1080p or 4K — the same high-resolution pass behind the Flow UI's HD download. Accepts a media ID or a local video path already in Flow history.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "media_id": {"type": "string", "description": "Media ID of a finished Flow video, or a local video path/filename tracked in history"},
                    "resolution": {
                        "type": "string",
                        "enum": ["1080p", "4k"],
                        "default": "1080p",
                        "description": "Target resolution: '1080p' is free, '4k' costs credits and needs a higher Flow tier"
                    },
                    "aspect": {"type": "string", "enum": ["landscape", "portrait"], "default": "landscape", "description": "Aspect ratio of the source video"},
                    "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "description": "Optional explicit upsampler seed"}
                },
                "required": ["media_id"]
            }
        },
        {
            "name": "generate_flow_sequence",
            "description": "Generate a continuity-chained run of shots: each shot starts on the previous shot's final frame, so cuts land on matching pixels. Returns clip paths in order. Requires FFmpeg.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "shots": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 60,
                        "description": "Ordered shots. Each item is either a prompt string or {prompt, duration, end_image_path}.",
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "prompt": {"type": "string"},
                                        "duration": {"type": "integer", "enum": [4, 6, 8, 10]},
                                        "end_image_path": {"type": "string"}
                                    },
                                    "required": ["prompt"]
                                }
                            ]
                        }
                    },
                    "aspect": {"type": "string", "enum": ["landscape", "portrait"], "default": "landscape"},
                    "duration": {"type": "integer", "enum": [4, 6, 8, 10], "default": 8, "description": "Default duration for shots that don't set their own"},
                    "start_image_path": {"type": "string", "description": "Optional opening frame for shot 1; later shots chain automatically"},
                    "output_dir": {"type": "string", "description": "Where clips are written; defaults to ~/Downloads/Flow-Agent"},
                    "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "description": "Base seed; each shot offsets from it"},
                    "video_model": {"type": "string", "description": "Override the Flow videoModelKey"},
                    "ref_media_ids": {
                        "type": "array", "items": {"type": "string"}, "maxItems": 10,
                        "description": "Reference media applied to every shot, for style or character carry-through"
                    },
                    "resolution": {
                        "type": "string",
                        "enum": ["720p", "1080p", "4k"],
                        "default": "720p",
                        "description": "Delivery resolution for every shot in the sequence"
                    }
                },
                "required": ["shots"]
            }
        },
        {
            "name": "extract_video_frame",
            "description": "Extract one frame from a local video as a PNG. Use the last frame of a clip as the start image of the next to chain shots manually. Requires FFmpeg.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "video_path": {"type": "string", "description": "Local video file"},
                    "position": {"type": "string", "default": "last", "description": "'first', 'last', or a frame index like '48'"},
                    "output_path": {"type": "string", "description": "Optional PNG destination"}
                },
                "required": ["video_path"]
            }
        },
        {
            "name": "concat_flow_videos",
            "description": "Concatenate local clips in order into one MP4, optionally replacing audio with a single narration track. Requires FFmpeg.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "video_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "Clips in playback order"},
                    "output_path": {"type": "string", "description": "Destination MP4"},
                    "audio_path": {"type": "string", "description": "Optional audio track replacing per-clip audio (e.g. one narration voice)"},
                    "mute_source": {"type": "boolean", "default": False, "description": "Drop source audio when no audio_path is given"}
                },
                "required": ["video_paths", "output_path"]
            }
        },
        {
            "name": "upload_flow_media",
            "description": "Upload an image or video to Google Flow from a local path, a public URL, or base64 data, and return its media ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute local image/video path"},
                    "image_url": {"type": "string", "description": "Optional public HTTP(S) URL of the image/video to fetch and upload"},
                    "image_base64": {"type": "string", "description": "Optional base64-encoded image/video payload"}
                }
            }
        },
        {
            "name": "download_media_from_url",
            "description": "Download an image/video from an HTTP(S) URL, including redirects and signed links; optionally upload it to Google Flow.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Direct or signed HTTP(S) image/video URL"},
                    "output_dir": {"type": "string", "description": "Optional local destination directory; defaults to FLOW_OUTPUT_DIR"},
                    "filename": {"type": "string", "description": "Optional output filename"},
                    "upload_to_flow": {"type": "boolean", "default": False, "description": "Upload the downloaded file to Google Flow and return its media ID"},
                    "max_size_mb": {"type": "integer", "minimum": 1, "maximum": 4096, "default": 2048}
                },
                "required": ["url"]
            }
        },
        {
            "name": "edit_flow_video",
            "description": "Edit an existing Flow video (video-to-video) using its media ID or a local video file.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Requested video transformation"},
                    "media_id": {"type": "string", "description": "Existing Flow video media ID"},
                    "video_path": {"type": "string", "description": "Optional local video; uploaded automatically when media_id is omitted"},
                    "aspect": {"type": "string", "enum": ["landscape", "portrait"], "default": "landscape"},
                    "duration": {"type": "integer", "enum": [4, 6, 8, 10], "default": 8},
                    "ref_media_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 10}
                },
                "required": ["prompt"]
            }
        }
    ]

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "tools": tools
        }
    }

def call_get_flow_credits():
    try:
        url = f"{FLOW_API_URL}/v1/credits"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                # New fan-out format: total across all connected browsers.
                if isinstance(data, dict) and "total_credits" in data:
                    total = data.get("total_credits", 0)
                    clients = data.get("clients", [])
                    ok = sum(1 for c in clients if c.get("ok"))
                    failed = len(clients) - ok
                    line = (f"Total Google Flow credits across all connected browsers: {total} "
                            f"({ok} clients ready" + (f", {failed} failed" if failed else "") + ")")
                    return line
                # Single-client format (X-Client-Id or legacy).
                inner = data.get("data", data) if isinstance(data, dict) else {}
                credits = inner.get("credits", "unknown")
                return f"Remaining Google Flow credits/generations: {credits}"
            return f"Error from Flow API ({response.status})"
    except urllib.error.HTTPError as e:
        try:
            err_msg = e.read().decode('utf-8')
        except Exception:
            err_msg = str(e)
        return f"Error from Flow API ({e.code}): {err_msg}"
    except Exception as e:
        return f"Failed to connect to Flow Agent server: {str(e)}"

def call_get_flow_status():
    try:
        data = _request_json("/health", timeout=10)
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Failed to connect to Flow Agent server: {str(e)}"

def call_list_flow_models():
    try:
        data = _request_json("/v1/models", timeout=10)
        models = [item.get("id") for item in data.get("data", [])]
        return "Available image models: " + ", ".join(models) + f". Default: {_normalise_model(None)}"
    except Exception as e:
        return f"Failed to list Flow models: {str(e)}"

def call_get_flow_history(limit=20):
    try:
        limit = max(1, min(100, int(limit or 20)))
        data = _request_json("/v1/history", timeout=15)
        history = data.get("history", [])[:limit]
        return json.dumps({"count": len(history), "history": history}, indent=2)
    except Exception as e:
        return f"Failed to get Flow history: {str(e)}"

def call_upload_flow_media(file_path=None, image_url=None, image_base64=None):
    """Upload to Flow from a local path, a public URL, or raw base64.

    Mirrors flow_server/mcp/tools.py's upload_flow_media schema so stdio and SSE
    clients can use the same three input forms.
    """
    if not file_path and not image_url and not image_base64:
        return {"error": "Provide one of 'file_path', 'image_url', or 'image_base64'."}

    scratch = None
    try:
        if file_path:
            payload = _file_data_uri(file_path)
        elif image_base64:
            payload = image_base64
        else:
            # Scratch dir, so a URL basename can never collide with — and then
            # delete — a real generated asset.
            scratch = tempfile.mkdtemp(prefix="flow-upload-")
            downloaded = call_download_media_from_url(image_url, output_dir=scratch)
            if downloaded.get("error"):
                return downloaded
            payload = _file_data_uri(downloaded["path"])

        return _request_json("/v1/upload", {"image_base64": payload}, timeout=300)
    except urllib.error.HTTPError as e:
        return {"error": f"Upload failed ({e.code}): {e.read().decode('utf-8', errors='replace')}"}
    except Exception as e:
        return {"error": f"Upload failed: {str(e)}"}
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)

def call_generate_flow_image(prompt, size="1280x720", count=1, ref_image_path=None,
                             ref_image_paths=None, ref_media_ids=None, model=None,
                             seed=None):
    if not prompt or not str(prompt).strip():
        return "Error: 'prompt' is required and cannot be empty.", None
    prompt = str(prompt).strip()
    payload = {
        "prompt": prompt,
        "size": size,
        "n": max(1, min(20, int(count or 1))),
        # Ask the backend to return the image bytes inline. The backend does one
        # robust (proxy-bypassing, retried) download; MCP never has to reach the
        # remote CDN itself, so there's a single point of failure, not two.
        "response_format": "b64_json"
    }
    payload["model"] = _normalise_model(model)
    if seed is not None:
        payload["seed"] = int(seed)

    media_ids = list(ref_media_ids or [])
    local_refs = list(ref_image_paths or [])
    if ref_image_path:
        local_refs.insert(0, ref_image_path)
    for path in local_refs[:10]:
        uploaded = call_upload_flow_media(path)
        if uploaded.get("error"):
            return uploaded["error"], []
        if uploaded.get("media_id"):
            media_ids.append(uploaded["media_id"])
    if media_ids:
        payload["ref_media_ids"] = media_ids[:10]

    try:
        log_debug(f"Sending image generation request for prompt: {prompt}")
        url = f"{FLOW_API_URL}/v1/images/generations"
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=300) as response:
            if response.status != 200:
                return f"Error generating image ({response.status})", []
            res_data = json.loads(response.read().decode("utf-8"))
            data = res_data.get("data", [])
            if not data:
                return "No images returned by Flow Agent.", []
            images = []
            urls = []
            for item in data:
                image_data_b64 = item.get("b64_json")
                img_url = item.get("url")
                if not image_data_b64 and img_url:
                    body = _download_bytes(img_url, timeout=60)
                    if body:
                        image_data_b64 = base64.b64encode(body).decode("utf-8")
                if image_data_b64:
                    images.append(image_data_b64)
                if img_url:
                    urls.append(img_url)
            text = f"Success! Generated {len(data)} image(s) with model {payload['model']}."
            if urls:
                text += "\nURLs:\n" + "\n".join(urls)
            return text, images
    except urllib.error.HTTPError as e:
        try:
            err_msg = e.read().decode('utf-8')
        except Exception:
            err_msg = str(e)
        return f"Error generating image ({e.code}): {err_msg}", []
    except Exception as e:
        return f"Failed to communicate with Flow Agent server: {str(e)}", []

def call_generate_flow_video(prompt, aspect="landscape", start_image_path=None, duration=8,
                             count=1, start_media_id=None, ref_media_ids=None, is_video=False,
                             seed=None, end_image_path=None, end_media_id=None, video_model=None,
                             resolution=None):
    if not prompt or not str(prompt).strip():
        return "Error: 'prompt' is required and cannot be empty."
    prompt = str(prompt).strip()
    payload = {
        "prompt": prompt,
        "aspect": aspect,
        "n": max(1, min(20, int(count or 1))),
        "duration": int(duration or 8)
    }
    if start_media_id:
        payload["start_media_id"] = start_media_id
        payload["is_video"] = bool(is_video)
    if ref_media_ids:
        payload["ref_media_ids"] = list(ref_media_ids)[:10]

    if seed is not None:
        payload["seed"] = int(seed)
    if video_model:
        payload["video_model"] = video_model
    if resolution:
        payload["resolution"] = resolution
    if end_media_id:
        payload["end_media_id"] = end_media_id

    if start_image_path:
        if not os.path.exists(start_image_path):
            return f"Error: Starting image path does not exist: {start_image_path}"
        try:
            payload["image_base64"] = _file_data_uri(start_image_path)
        except Exception as e:
            return f"Error reading starting image: {str(e)}"

    if end_image_path:
        if not os.path.exists(end_image_path):
            return f"Error: End image path does not exist: {end_image_path}"
        try:
            payload["end_image_base64"] = _file_data_uri(end_image_path)
        except Exception as e:
            return f"Error reading end image: {str(e)}"

    try:
        log_debug(f"Sending video generation request for prompt: {prompt}")
        url = f"{FLOW_API_URL}/v1/videos/generations"
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=600) as response:
            if response.status != 200:
                return f"Error generating video ({response.status})"
            res_data = json.loads(response.read().decode("utf-8"))
            data = res_data.get("data", [])
            if not data:
                return "No videos returned by Flow Agent."
            urls = [item.get("url") for item in data if item.get("url")]
            media_ids = [item.get("media_id") for item in data if item.get("media_id")]
            resolutions = [item.get("resolution") for item in data if item.get("resolution")]
            summary = ""
            if resolutions:
                ordered = sorted(set(resolutions), key=resolutions.index)
                summary = " Resolution: " + ", ".join(ordered) + "."
            note = res_data.get("note")
            return (f"Success! Generated {len(data)} video(s)." + summary
                    + ("\nURLs:\n" + "\n".join(urls) if urls else "")
                    + ("\nMedia IDs: " + ", ".join(media_ids) if media_ids else "")
                    + (f"\nNote: {note}" if note else ""))
    except urllib.error.HTTPError as e:
        try:
            err_msg = e.read().decode('utf-8')
        except Exception:
            err_msg = str(e)
        return f"Error generating video ({e.code}): {err_msg}"
    except Exception as e:
        return f"Failed to communicate with Flow Agent server: {str(e)}"

def call_upsample_flow_video(media_id, resolution="1080p", aspect="landscape", seed=None):
    """Upsample a finished Flow video to 1080p or 4K through the backend."""
    if not media_id or not str(media_id).strip():
        return "Error: 'media_id' is required."
    payload = {
        "media_id": str(media_id).strip(),
        "resolution": str(resolution or "1080p"),
        "aspect": aspect,
    }
    if seed is not None:
        payload["seed"] = int(seed)

    try:
        log_debug(f"Requesting {payload['resolution']} upsample for {payload['media_id']}")
        req = urllib.request.Request(
            f"{FLOW_API_URL}/v1/videos/upsample",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=900) as response:
            if response.status != 200:
                return f"Error upsampling video ({response.status})"
            res_data = json.loads(response.read().decode("utf-8"))
            data = res_data.get("data", [])
            if not data:
                return "No upsampled video returned by Flow Agent."
            item = data[0]
            return (f"Success! Upsampled to {item.get('resolution') or payload['resolution']}."
                    + (f"\nURL: {item['url']}" if item.get("url") else "")
                    + (f"\nMedia ID: {item['media_id']}" if item.get("media_id") else "")
                    + (f"\nUpsampled from: {item['source_media_id']}"
                       if item.get("source_media_id") else ""))
    except urllib.error.HTTPError as e:
        try:
            err_msg = e.read().decode("utf-8")
        except Exception:
            err_msg = str(e)
        return f"Error upsampling video ({e.code}): {err_msg}"
    except Exception as e:
        return f"Failed to communicate with Flow Agent server: {str(e)}"


def call_edit_flow_video(prompt, media_id=None, video_path=None, aspect="landscape",
                         duration=8, ref_media_ids=None):
    if not media_id and video_path:
        uploaded = call_upload_flow_media(video_path)
        if uploaded.get("error"):
            return uploaded["error"]
        media_id = uploaded.get("media_id")
    if not media_id:
        return "Error: provide media_id or video_path."
    return call_generate_flow_video(
        prompt, aspect=aspect, duration=duration, count=1,
        start_media_id=media_id, ref_media_ids=ref_media_ids, is_video=True,
    )

def call_download_media_from_url(url, output_dir=None, filename=None,
                                 upload_to_flow=False, max_size_mb=2048):
    """Download a remote media asset safely and optionally upload it to Flow."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"error": "Only valid http:// or https:// URLs are supported."}

    try:
        max_bytes = max(1, min(4096, int(max_size_mb or 2048))) * 1024 * 1024
    except (TypeError, ValueError):
        max_bytes = 2048 * 1024 * 1024

    destination = os.path.abspath(os.path.expanduser(output_dir or OUTPUT_DIR))
    os.makedirs(destination, exist_ok=True)
    request = urllib.request.Request(
        str(url).strip(),
        headers={"User-Agent": "Mozilla/5.0 (Flow Agent MCP)", "Accept": "image/*,video/*,application/octet-stream,*/*"},
    )
    temp_path = None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=120) as response:
            declared_type = response.headers.get("Content-Type", "")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                return {"error": f"Download exceeds max_size_mb ({max_size_mb} MB)."}

            url_name = _safe_filename(os.path.basename(parsed.path))
            output_name = _safe_filename(filename or url_name)
            fd, temp_path = tempfile.mkstemp(prefix=".flow-download-", dir=destination)
            total = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        return {"error": f"Download exceeded max_size_mb ({max_size_mb} MB)."}
                    out.write(chunk)
            content_type = sniff_media_type(
                temp_path, declared_mime=declared_type, filename=url_name
            )
            actual_extension = extension_for_media(
                temp_path, declared_mime=declared_type, filename=url_name
            )
            if actual_extension:
                stem, existing_extension = os.path.splitext(output_name)
                if existing_extension.lower() != actual_extension:
                    output_name = (stem if existing_extension else output_name) + actual_extension
            final_path = os.path.join(destination, output_name)
            os.replace(temp_path, final_path)
            temp_path = None

        if content_type.startswith(("image/", "video/")):
            record_local_media(
                final_path,
                mime_type=content_type,
                prompt=f"Downloaded from {parsed.netloc}",
                source="download",
            )

        result = {
            "success": True,
            "path": final_path,
            "bytes": total,
            "content_type": content_type,
            "source_host": parsed.netloc,
        }
        if upload_to_flow:
            result["flow_upload"] = call_upload_flow_media(final_path)
        return result
    except urllib.error.HTTPError as e:
        return {"error": f"Download failed ({e.code}): {e.reason}"}
    except Exception as e:
        return {"error": f"Download failed: {str(e)}"}
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

# ─── Local media tooling ─────────────────────────────────────
# Shot-to-shot continuity is built by carrying the last frame of one clip into
# the next as its start image, so these helpers need frame-accurate local
# access to the generated files. FFmpeg is optional: only the tools below
# require it, and they fail with a clear message rather than at import time.

def _require_ffmpeg():
    missing = [n for n in ("ffmpeg", "ffprobe") if not shutil.which(n)]
    if missing:
        return ("Error: %s not found on PATH. Install FFmpeg to use this tool."
                % " and ".join(missing))
    return None


def _media_dir(output_dir=None):
    target = output_dir or os.path.join(os.path.expanduser("~"), "Downloads", "Flow-Agent")
    os.makedirs(target, exist_ok=True)
    return target


def _run(cmd, timeout=180):
    import subprocess
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def call_extract_video_frame(video_path, position="last", output_path=None):
    """Pull one frame out of a local video. 'position' is first, last, or a frame index."""
    err = _require_ffmpeg()
    if err:
        return {"error": err}
    if not video_path or not os.path.exists(video_path):
        return {"error": f"Video not found: {video_path}"}

    if not output_path:
        base = os.path.splitext(os.path.basename(video_path))[0]
        output_path = os.path.join(os.path.dirname(video_path), f"{base}_{position}.png")

    pos = str(position).lower()
    if pos == "last":
        # Seeking from the end is far cheaper than decoding the whole file, and
        # -update 1 keeps overwriting so the final decoded frame is what lands.
        cmd = ["ffmpeg", "-y", "-v", "error", "-sseof", "-0.2", "-i", video_path,
               "-update", "1", output_path]
    elif pos == "first":
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
               "-vf", "select=eq(n\\,0)", "-frames:v", "1", output_path]
    else:
        try:
            idx = int(position)
        except (TypeError, ValueError):
            return {"error": "position must be 'first', 'last', or a frame index"}
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
               "-vf", f"select=eq(n\\,{idx})", "-frames:v", "1", output_path]

    try:
        code, _, stderr = _run(cmd)
    except Exception as e:
        return {"error": f"Frame extraction failed: {e}"}
    if code != 0 or not os.path.exists(output_path):
        return {"error": f"Frame extraction failed: {stderr.strip()[:400]}"}
    return {"frame_path": output_path}


def _post_video_json(payload, timeout=900):
    """Submit one video generation and return (items, error)."""
    try:
        req = urllib.request.Request(
            f"{FLOW_API_URL}/v1/videos/generations",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                return None, f"HTTP {response.status}"
            return json.loads(response.read().decode("utf-8")).get("data", []), None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = str(e)
        return None, f"HTTP {e.code}: {detail[:400]}"
    except Exception as e:
        return None, str(e)


def call_generate_flow_sequence(shots, aspect="landscape", duration=8, output_dir=None,
                                start_image_path=None, seed=None, video_model=None,
                                ref_media_ids=None, resolution=None):
    """Generate a continuity-chained run of shots.

    Each shot after the first starts on the previous shot's final frame, so cuts
    land on matching pixels instead of relying on the model to re-imagine the
    scene. Returns the clip paths in order, ready to concatenate.
    """
    err = _require_ffmpeg()
    if err:
        return {"error": err}
    if not shots or not isinstance(shots, list):
        return {"error": "'shots' must be a non-empty list of prompts or shot objects"}

    target_dir = _media_dir(output_dir)
    carry_frame = start_image_path
    if carry_frame and not os.path.exists(carry_frame):
        return {"error": f"start_image_path does not exist: {carry_frame}"}

    clips, failures = [], []
    for i, shot in enumerate(shots):
        if isinstance(shot, dict):
            prompt = shot.get("prompt")
            shot_duration = int(shot.get("duration") or duration)
            end_path = shot.get("end_image_path")
        else:
            prompt, shot_duration, end_path = str(shot), int(duration), None
        if not prompt or not str(prompt).strip():
            failures.append({"index": i, "error": "empty prompt"})
            break

        payload = {"prompt": str(prompt).strip(), "aspect": aspect,
                   "n": 1, "duration": shot_duration}
        if seed is not None:
            payload["seed"] = int(seed) + i
        if video_model:
            payload["video_model"] = video_model
        if ref_media_ids:
            payload["ref_media_ids"] = list(ref_media_ids)[:10]
        if resolution:
            payload["resolution"] = resolution
        if carry_frame:
            try:
                payload["image_base64"] = _file_data_uri(carry_frame)
            except Exception as e:
                failures.append({"index": i, "error": f"could not read carry frame: {e}"})
                break
        if end_path:
            if not os.path.exists(end_path):
                failures.append({"index": i, "error": f"end_image_path missing: {end_path}"})
                break
            payload["end_image_base64"] = _file_data_uri(end_path)

        items, gen_err = _post_video_json(payload)
        if gen_err or not items:
            failures.append({"index": i, "error": gen_err or "no media returned"})
            break

        url = items[0].get("url")
        body = _download_bytes(url, timeout=180) if url else None
        if not body:
            failures.append({"index": i, "error": f"download failed for {url}"})
            break
        clip_path = os.path.join(target_dir, f"seq_{i + 1:02d}.mp4")
        with open(clip_path, "wb") as f:
            f.write(body)
        clips.append({"index": i, "path": clip_path,
                      "media_id": items[0].get("media_id"), "prompt": prompt})

        # Carry this clip's final frame into the next shot.
        if i < len(shots) - 1:
            extracted = call_extract_video_frame(clip_path, "last",
                                                 os.path.join(target_dir, f"seq_{i + 1:02d}_last.png"))
            if extracted.get("error"):
                failures.append({"index": i, "error": extracted["error"]})
                break
            carry_frame = extracted["frame_path"]

    return {"clips": clips, "generated": len(clips), "requested": len(shots),
            "failures": failures, "output_dir": target_dir}


def call_concat_flow_videos(video_paths, output_path, audio_path=None, mute_source=False):
    """Concatenate clips in order, optionally replacing audio with one track."""
    err = _require_ffmpeg()
    if err:
        return {"error": err}
    if not video_paths or not isinstance(video_paths, list) or len(video_paths) < 1:
        return {"error": "'video_paths' must be a non-empty list"}
    missing = [p for p in video_paths if not os.path.exists(p)]
    if missing:
        return {"error": f"Missing input files: {missing[:5]}"}
    if not output_path:
        return {"error": "'output_path' is required"}

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    list_file = os.path.join(_media_dir(), f"concat_{uuid.uuid4().hex[:8]}.txt")
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for p in video_paths:
                f.write("file '%s'\n" % os.path.abspath(p).replace("\\", "/").replace("'", "'\\''"))

        # Re-encode rather than stream-copy: clips can differ in SAR/timebase and
        # a copy-concat would silently desync or refuse.
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_file]
        if audio_path:
            if not os.path.exists(audio_path):
                return {"error": f"audio_path does not exist: {audio_path}"}
            cmd += ["-i", audio_path, "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        elif mute_source:
            cmd += ["-an"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium"]
        if audio_path:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += [output_path]

        code, _, stderr = _run(cmd, timeout=1800)
        if code != 0 or not os.path.exists(output_path):
            return {"error": f"Concat failed: {stderr.strip()[:400]}"}
        size_mb = round(os.path.getsize(output_path) / (1024 * 1024), 2)
        return {"output_path": output_path, "clips": len(video_paths), "size_mb": size_mb}
    except Exception as e:
        return {"error": f"Concat failed: {e}"}
    finally:
        try:
            os.remove(list_file)
        except Exception:
            pass


def handle_tool_call(request_id, tool_name, arguments):
    log_debug(f"Calling tool: {tool_name} with args: {arguments}")

    if tool_name == "get_flow_status":
        content = [{"type": "text", "text": call_get_flow_status()}]
    elif tool_name == "get_flow_credits":
        text = call_get_flow_credits()
        content = [{"type": "text", "text": text}]
    elif tool_name == "list_flow_models":
        content = [{"type": "text", "text": call_list_flow_models()}]
    elif tool_name == "get_flow_history":
        content = [{"type": "text", "text": call_get_flow_history(arguments.get("limit", 20))}]
    elif tool_name == "generate_flow_image":
        prompt = arguments.get("prompt")
        size = arguments.get("size", "1280x720")
        count = arguments.get("count", 1)
        ref_image_path = arguments.get("ref_image_path")
        ref_image_paths = arguments.get("ref_image_paths")
        ref_media_ids = arguments.get("ref_media_ids")
        model = arguments.get("model")
        text, images_b64 = call_generate_flow_image(
            prompt, size, count, ref_image_path, ref_image_paths, ref_media_ids, model,
            arguments.get("seed"),
        )
        content = [{"type": "text", "text": text}]
        for image_data_b64 in images_b64:
            try:
                image_mime = sniff_media_type(base64.b64decode(image_data_b64))
            except Exception:
                image_mime = "application/octet-stream"
            content.append({
                "type": "image",
                "data": image_data_b64,
                "mimeType": image_mime
            })
    elif tool_name == "generate_flow_video":
        prompt = arguments.get("prompt")
        aspect = arguments.get("aspect", "landscape")
        start_image_path = arguments.get("start_image_path")
        text = call_generate_flow_video(
            prompt,
            aspect,
            start_image_path,
            arguments.get("duration", 8),
            arguments.get("count", 1),
            arguments.get("start_media_id"),
            arguments.get("ref_media_ids"),
            arguments.get("is_video", False),
            arguments.get("seed"),
            arguments.get("end_image_path"),
            arguments.get("end_media_id"),
            arguments.get("video_model"),
            arguments.get("resolution"),
        )
        content = [{"type": "text", "text": text}]
    elif tool_name == "upsample_flow_video":
        text = call_upsample_flow_video(
            arguments.get("media_id"),
            arguments.get("resolution", "1080p"),
            arguments.get("aspect", "landscape"),
            arguments.get("seed"),
        )
        content = [{"type": "text", "text": text}]
    elif tool_name == "generate_flow_sequence":
        result = call_generate_flow_sequence(
            arguments.get("shots"),
            arguments.get("aspect", "landscape"),
            arguments.get("duration", 8),
            arguments.get("output_dir"),
            arguments.get("start_image_path"),
            arguments.get("seed"),
            arguments.get("video_model"),
            arguments.get("ref_media_ids"),
            arguments.get("resolution"),
        )
        content = [{"type": "text", "text": json.dumps(result, indent=2)}]
    elif tool_name == "extract_video_frame":
        result = call_extract_video_frame(
            arguments.get("video_path"),
            arguments.get("position", "last"),
            arguments.get("output_path"),
        )
        content = [{"type": "text", "text": json.dumps(result, indent=2)}]
    elif tool_name == "concat_flow_videos":
        result = call_concat_flow_videos(
            arguments.get("video_paths"),
            arguments.get("output_path"),
            arguments.get("audio_path"),
            arguments.get("mute_source", False),
        )
        content = [{"type": "text", "text": json.dumps(result, indent=2)}]
    elif tool_name == "upload_flow_media":
        result = call_upload_flow_media(
            arguments.get("file_path"),
            arguments.get("image_url"),
            arguments.get("image_base64"),
        )
        content = [{"type": "text", "text": json.dumps(result, indent=2)}]
    elif tool_name == "download_media_from_url":
        result = call_download_media_from_url(
            arguments.get("url"),
            arguments.get("output_dir"),
            arguments.get("filename"),
            arguments.get("upload_to_flow", False),
            arguments.get("max_size_mb", 2048),
        )
        content = [{"type": "text", "text": json.dumps(result, indent=2)}]
    elif tool_name == "edit_flow_video":
        text = call_edit_flow_video(
            arguments.get("prompt"),
            arguments.get("media_id"),
            arguments.get("video_path"),
            arguments.get("aspect", "landscape"),
            arguments.get("duration", 8),
            arguments.get("ref_media_ids"),
        )
        content = [{"type": "text", "text": text}]
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {tool_name}"
            }
        }

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": content
        }
    }

def main():
    log_debug("Flow MCP Server Started on Stdin/Stdout.")
    log_debug(f"Backend: {FLOW_API_URL}")

    while True:
        request_id = None
        try:
            line = sys.stdin.readline()
            if not line:
                break
            if not line.strip():
                continue

            log_debug(f"Received raw line: {line.strip()}")
            message = json.loads(line)
            method = message.get("method")
            request_id = message.get("id")

            if method == "initialize":
                response = handle_initialize(request_id, message.get("params"))
            elif method in ("initialized", "notifications/initialized"):
                # Notification, no response needed
                continue
            elif method == "tools/list":
                response = handle_tools_list(request_id)
            elif method == "tools/call":
                params = message.get("params", {})
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                response = handle_tool_call(request_id, tool_name, arguments)
            elif method == "ping":
                response = {"jsonrpc": "2.0", "id": request_id, "result": {}}
            elif request_id is None:
                # Unknown notification (no id) — nothing to answer.
                continue
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}"
                    }
                }

            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

        except json.JSONDecodeError:
            # Can't recover an id from unparseable input; log and move on.
            log_debug("Failed to decode JSON from stdin.")
        except Exception as e:
            # Never hang the client: if a request had an id, always answer it.
            log_debug(f"Main loop exception: {str(e)}")
            if request_id is not None:
                try:
                    sys.stdout.write(json.dumps({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": f"Internal error: {str(e)}"}
                    }) + "\n")
                    sys.stdout.flush()
                except Exception:
                    pass

if __name__ == "__main__":
    main()
