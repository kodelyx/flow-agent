#!/usr/bin/env python3
import sys
import json
import base64
import os
import urllib.request
import urllib.error

# Load config.env so this MCP process sees the same ports/settings as the
# backend, even when launched standalone by an AI client.
def _load_env_files():
    if getattr(sys, 'frozen', False):
        root = os.path.dirname(sys.executable)
    else:
        root = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root, "config.env")
    if not os.path.exists(path):
        return
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
# so MCP keeps working even if the port is changed in config.env.
_API_HOST = os.environ.get("OPENAI_API_HOST", "127.0.0.1")
_API_PORT = os.environ.get("OPENAI_API_PORT", "8001")
FLOW_API_URL = os.environ.get("FLOW_API_URL", f"http://{_API_HOST}:{_API_PORT}")

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
                "name": "flow-mcp",
                "version": "1.0.0"
            }
        }
    }
    return response

def handle_tools_list(request_id):
    tools = [
        {
            "name": "get_flow_credits",
            "description": "Check the remaining credits / generations on the logged-in Google Flow account.",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        },
        {
            "name": "generate_flow_image",
            "description": "Generate an image using Google Flow. Optionally supports reference images (Image-to-Image).",
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
                    "ref_image_path": {
                        "type": "string",
                        "description": "Optional local file path to a reference image on the host for Image-to-Image"
                    },
                    "model": {
                        "type": "string",
                        "description": "Image model to use (harbor_seal/lite, narwhal/standard, gem_pix_2/pro)",
                        "default": "harbor_seal"
                    }
                },
                "required": ["prompt"]
            }
        },
        {
            "name": "generate_flow_video",
            "description": "Generate a cinematic video clip using Google Flow (4/6/8/10 seconds). Optionally supports a starting frame reference image (Image-to-Video).",
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
                    "duration": {
                        "type": "integer",
                        "description": "Video duration in seconds (default: 10)",
                        "enum": [4, 6, 8, 10],
                        "default": 10
                    },
                    "start_image_path": {
                        "type": "string",
                        "description": "Optional local file path to a starting reference image on the host for Image-to-Video"
                    }
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
                credits = data.get("credits", "unknown")
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

def call_generate_flow_image(prompt, size="1280x720", ref_image_path=None, model=None):
    if not prompt or not str(prompt).strip():
        return "Error: 'prompt' is required and cannot be empty.", None
    prompt = str(prompt).strip()
    payload = {
        "prompt": prompt,
        "size": size,
        "n": 1,
        # Ask the backend to return the image bytes inline. The backend does one
        # robust (proxy-bypassing, retried) download; MCP never has to reach the
        # remote CDN itself, so there's a single point of failure, not two.
        "response_format": "b64_json"
    }
    if model:
        payload["model"] = model

    if ref_image_path:
        if not os.path.exists(ref_image_path):
            return f"Error: Reference image path does not exist: {ref_image_path}", None
        try:
            with open(ref_image_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")
                payload["image_base64"] = f"data:image/png;base64,{encoded}"
        except Exception as e:
            return f"Error reading reference image: {str(e)}", None

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
                return f"Error generating image ({response.status})", None
            res_data = json.loads(response.read().decode("utf-8"))
            data = res_data.get("data", [])
            if not data:
                return "No images returned by Flow Agent.", None
            first = data[0]

            # Preferred path: backend already returned the bytes inline.
            image_data_b64 = first.get("b64_json")
            img_url = first.get("url")

            # Fallback: only a URL came back (older backend / b64 unsupported).
            # Try to fetch it ourselves, bypassing any proxy.
            if not image_data_b64 and img_url:
                log_debug(f"No inline bytes; downloading from URL: {img_url}")
                body = _download_bytes(img_url, timeout=60)
                if body:
                    image_data_b64 = base64.b64encode(body).decode("utf-8")

            if not image_data_b64 and not img_url:
                return f"Image generated but no data or URL was returned. Raw: {json.dumps(first)[:300]}", None

            if image_data_b64:
                location = f"\nURL: {img_url}" if img_url else ""
                return f"Success! Image generated successfully.{location}", image_data_b64
            # Have a URL but couldn't materialise bytes for inline preview.
            return (f"Success! Image generated successfully.\nURL: {img_url}"
                    f"\n(Note: inline preview unavailable; open the URL to view.)"), None
    except urllib.error.HTTPError as e:
        try:
            err_msg = e.read().decode('utf-8')
        except Exception:
            err_msg = str(e)
        return f"Error generating image ({e.code}): {err_msg}", None
    except Exception as e:
        return f"Failed to communicate with Flow Agent server: {str(e)}", None

def call_generate_flow_video(prompt, aspect="landscape", start_image_path=None, duration=10):
    if not prompt or not str(prompt).strip():
        return "Error: 'prompt' is required and cannot be empty."
    prompt = str(prompt).strip()
    allowed_durations = {4, 6, 8, 10}
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 10
    if duration not in allowed_durations:
        return f"Error: duration must be one of {sorted(allowed_durations)}, got {duration}"
    payload = {
        "prompt": prompt,
        "aspect": aspect,
        "n": 1,
        "duration": duration,
    }
    
    if start_image_path:
        if not os.path.exists(start_image_path):
            return f"Error: Starting image path does not exist: {start_image_path}"
        try:
            with open(start_image_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")
                payload["image_base64"] = f"data:image/png;base64,{encoded}"
        except Exception as e:
            return f"Error reading starting image: {str(e)}"
            
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
            vid_url = data[0].get("url")
            if not vid_url:
                return f"Video generated but no URL was returned. Raw: {json.dumps(data[0])[:300]}"
            return f"Success! Video generated successfully.\nURL: {vid_url}"
    except urllib.error.HTTPError as e:
        try:
            err_msg = e.read().decode('utf-8')
        except Exception:
            err_msg = str(e)
        return f"Error generating video ({e.code}): {err_msg}"
    except Exception as e:
        return f"Failed to communicate with Flow Agent server: {str(e)}"

def handle_tool_call(request_id, tool_name, arguments):
    log_debug(f"Calling tool: {tool_name} with args: {arguments}")
    
    if tool_name == "get_flow_credits":
        text = call_get_flow_credits()
        content = [{"type": "text", "text": text}]
    elif tool_name == "generate_flow_image":
        prompt = arguments.get("prompt")
        size = arguments.get("size", "1280x720")
        ref_image_path = arguments.get("ref_image_path")
        model = arguments.get("model")
        text, image_data_b64 = call_generate_flow_image(prompt, size, ref_image_path, model)
        content = [{"type": "text", "text": text}]
        if image_data_b64:
            content.append({
                "type": "image",
                "data": image_data_b64,
                "mimeType": "image/png"
            })
                elif tool_name == "generate_flow_video":
                prompt = arguments.get("prompt")
                aspect = arguments.get("aspect", "landscape")
                start_image_path = arguments.get("start_image_path")
                duration = arguments.get("duration", 10)
                text = call_generate_flow_video(prompt, aspect, start_image_path, duration)
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
