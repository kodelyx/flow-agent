#!/usr/bin/env python3
"""OpenAI-Compatible API Server for Flow Agent.

Implements standard OpenAI API specs (e.g. /v1/images/generations)
for seamless integration with OpenAI clients and external tools (n8n, Dify, etc.).
"""

import os
import sys
import uuid
import time
import json
import base64
import logging
import asyncio
import urllib.request
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security, Depends, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

# Add parent dir to sys.path so omniflash can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omniflash import ExtensionBridge, DEFAULT_PROJECT
from omniflash.config import CREDITS_PER_VIDEO
from omniflash.generators.t2i import generate_image, download_image, _parse_image_results

# Setup logging (format configured centrally in omniflash/__init__.py, imported above)
log = logging.getLogger("omniflash.openai_api")

# Directories
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(ROOT_DIR, "output"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "http://localhost:8001"
).rstrip("/")


def public_url(filename: str) -> str:
    return f"{PUBLIC_BASE_URL}/download/{filename}"


def _content_type(filename: str) -> str:
    return "video/mp4" if filename.endswith(".mp4") else "image/png"


async def publish(filename: str, out_path: str):
    """Make a generated file web-accessible."""
    return public_url(filename), None


async def recover_orphan_response(data: dict, meta: dict):
    """Handle an extension response whose caller already timed out.

    A generation can succeed on Google Flow but arrive after the request
    timed out. Instead of dropping it, parse any generated media, download
    it, and record it in history so nothing is silently lost.
    """
    try:
        if data.get("status") != 200:
            log.info("Orphan response %s ignored (status=%s)", data.get("id"), data.get("status"))
            return
        # Only images arrive inline; videos are polled separately, so only
        # image generations are recoverable this way.
        if meta.get("captcha_action") != "IMAGE_GENERATION":
            return
        results = _parse_image_results(data.get("data", {}))
        results = [r for r in results if r.get("image_url")]
        if not results:
            return
        prompt = meta.get("prompt", "Recovered generation")
        timestamp = int(time.time())
        recovered = 0
        for i, r in enumerate(results):
            unique_id = uuid.uuid4().hex[:6]
            filename = f"flowagent_img_{timestamp}_{unique_id}_{i+1}.png"
            out_path = os.path.join(OUTPUT_DIR, filename)
            if not await download_image(bridge, r["image_url"], out_path):
                continue
            served_url, r2_key = await publish(filename, out_path)
            await append_to_history("image", served_url, prompt, r.get("media_id"), r2_key)
            recovered += 1
        if recovered:
            log.info("Recovered %d orphaned image(s) from late response %s", recovered, data.get("id"))
    except Exception:
        log.exception("Failed to recover orphan response")

# ExtensionBridge lifecycle
bridge: Optional[ExtensionBridge] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge
    log.info("Starting Flow Agent Extension Bridge (OpenAI Interface)...")
    bridge = ExtensionBridge()
    bridge.set_orphan_handler(recover_orphan_response)
    await bridge.start()

    # Run extension connection in background
    asyncio.create_task(bridge.wait_for_extension(timeout=30))

    yield

    log.info("Closing Flow Agent Extension Bridge...")
    if bridge:
        await bridge.close()

app = FastAPI(
    title="Flow Agent OpenAI API Wrapper",
    description="OpenAI-compatible endpoints for Google Flow AI image generation",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Authentication Dependency
security = HTTPBearer(auto_error=False)

async def verify_api_key(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    server_key = os.environ.get("SERVER_API_KEY")
    if not server_key:
        # If no key is defined in config.env, auth is skipped (disabled)
        return
    if not credentials or credentials.credentials != server_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Please pass 'Authorization: Bearer <key>'"
        )

# Helper to check bridge health
async def get_active_bridge() -> ExtensionBridge:
    global bridge
    if not bridge:
        raise HTTPException(status_code=503, detail="Extension bridge is not initialized")

    is_healthy = await bridge.health_check()
    if not is_healthy:
        log.info("Bridge health check failed. Re-waiting for extension connection...")
        connected = await bridge.wait_for_extension(timeout=10, max_retries=1)
        if not connected:
            raise HTTPException(
                status_code=503,
                detail="Google Flow extension is not connected. Open Google Flow in Chrome."
            )
    return bridge

# Map OpenAI image size to Flow aspect ratio
def map_size_to_aspect(size_str: Optional[str]) -> str:
    if not size_str:
        return "square"

    parts = size_str.lower().split("x")
    if len(parts) == 2:
        try:
            w, h = int(parts[0]), int(parts[1])
            ratio = w / h
            if 0.9 <= ratio <= 1.1:
                return "square"
            elif ratio > 1.4:
                return "landscape"
            elif ratio < 0.7:
                return "portrait"
            elif ratio > 1.0:
                return "4x3"
            else:
                return "3x4"
        except ValueError:
            pass
    return "square"


# OpenAI Request/Response Models
class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., description="The prompt to generate images from")
    model: str = Field("narwhal", description="Image model name (e.g. narwhal, gem_pix_2, imagen_4)")
    n: int = Field(1, ge=1, le=20, description="Number of images to generate (1-20)")
    size: str = Field("1024x1024", description="Image dimensions (e.g. 1024x1024, 1024x1792, etc.)")
    response_format: str = Field("url", description="The format in which the generated images are returned (url or b64_json)")
    user: Optional[str] = None
    image_base64: Optional[str] = Field(None, description="Optional base64 reference image for image-to-image")
    ref_media_ids: Optional[List[str]] = Field(None, description="Optional reference image media IDs (up to 10)")


class VideoGenerationRequest(BaseModel):
    prompt: str = Field(..., description="The prompt to generate videos from")
    aspect: str = Field("portrait", description="Video aspect ratio (portrait or landscape)")
    n: int = Field(1, ge=1, le=20, description="Number of videos to generate (1-20)")
    duration: int = Field(8, description="Duration in seconds (e.g. 4, 6, 8, 10)")
    image_base64: Optional[str] = Field(None, description="Optional base64 start image for image-to-video")
    ref_media_ids: Optional[List[str]] = Field(None, description="Optional reference image media IDs (up to 10)")
    start_media_id: Optional[str] = Field(None, description="Optional pre-uploaded start image or video media ID")
    is_video: Optional[bool] = Field(False, description="True if the pre-uploaded reference is a video")


# Extension WebSocket and Callback Endpoints
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log.info("Extension connecting via FastAPI WebSocket...")
    global bridge
    if bridge:
        await bridge.handle_fastapi_ws(websocket)
    else:
        log.error("Bridge not initialized")
        await websocket.close()

@app.post("/api/ext/callback")
async def http_callback(body: dict):
    global bridge
    if bridge:
        success = bridge.handle_http_callback(body)
        return {"ok": success}
    return {"ok": False}


# OpenAI Endpoints

@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    """List available Google Flow models (OpenAI format)."""
    return {
        "object": "list",
        "data": [
            {"id": "harbor_seal", "object": "model", "created": int(time.time()), "owned_by": "google"},
            {"id": "narwhal", "object": "model", "created": int(time.time()), "owned_by": "google"},
            {"id": "gem_pix_2", "object": "model", "created": int(time.time()), "owned_by": "google"}
        ]
    }


@app.post("/v1/images/generations", dependencies=[Depends(verify_api_key)])
async def openai_generate_image(req: ImageGenerationRequest):
    """Generate images from a prompt (OpenAI Spec)."""
    active_bridge = await get_active_bridge()
    aspect = map_size_to_aspect(req.size)
    project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

    ref_media_ids = req.ref_media_ids or None
    temp_img_path = None

    if req.image_base64 and not ref_media_ids:
        from omniflash.generators.i2v import upload_image
        b64_data = req.image_base64
        if "," in b64_data:
            b64_data = b64_data.split(",")[1]

        timestamp = int(time.time())
        temp_img_name = f"i2i_upload_{timestamp}_{uuid.uuid4().hex[:6]}.png"
        temp_img_path = os.path.join(OUTPUT_DIR, temp_img_name)

        try:
            with open(temp_img_path, "wb") as f:
                f.write(base64.b64decode(b64_data))

            media_id = await upload_image(active_bridge, temp_img_path, project_id)
            if media_id:
                ref_media_ids = [media_id]
            else:
                raise HTTPException(status_code=500, detail="Failed to upload I2I reference image to Google Flow.")
        except Exception as e:
            log.exception("Error uploading I2I reference image")
            if temp_img_path and os.path.exists(temp_img_path):
                os.remove(temp_img_path)
            raise HTTPException(status_code=500, detail=f"Image upload error: {str(e)}")

    # Trigger Flow generation chunk by chunk in parallel (Flow maximum count per request is 4)
    total_count = req.n
    chunks = []
    while total_count > 0:
        chunk_size = min(4, total_count)
        chunks.append(chunk_size)
        total_count -= chunk_size

    try:
        tasks = []
        for chunk_size in chunks:
            tasks.append(
                generate_image(
                    active_bridge,
                    prompt=req.prompt,
                    aspect=aspect,
                    project_id=project_id,
                    count=chunk_size,
                    ref_media_ids=ref_media_ids,
                    model=req.model
                )
            )

        # Run requests concurrently using asyncio.gather
        results_lists = await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        first_error = None
        for res in results_lists:
            if isinstance(res, Exception):
                first_error = res
                log.error(f"Error in parallel generate_image chunk: {res}")
            elif isinstance(res, list):
                results.extend(res)

        # If all requests failed, raise the exception
        if not results and first_error:
            raise first_error
    except Exception as e:
        if temp_img_path and os.path.exists(temp_img_path):
            try:
                os.remove(temp_img_path)
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))

    if temp_img_path and os.path.exists(temp_img_path):
        try:
            os.remove(temp_img_path)
        except Exception:
            pass

    if not results:
        raise HTTPException(status_code=400, detail="Flow failed to generate images.")

    data_outputs = []
    timestamp = int(time.time())

    for i, r in enumerate(results):
        url = r.get("image_url")
        if not url:
            continue

        unique_id = uuid.uuid4().hex[:6]
        filename = f"flowagent_img_{timestamp}_{unique_id}_{i+1}.png"
        out_path = os.path.join(OUTPUT_DIR, filename)

        download_success = await download_image(active_bridge, url, out_path)
        if not download_success:
            # Generation succeeded but the local download failed (e.g. transient
            # network/proxy block). Don't lose it — surface the remote URL and
            # record it in history so it stays recoverable.
            log.warning("Image %s generated but download failed; returning remote URL", r.get("media_id"))
            data_outputs.append({
                "url": url,
                "media_id": r.get("media_id"),
                "warning": "generated but local download failed; url is the remote Google Flow link",
            })
            await append_to_history("image", url, req.prompt, r.get("media_id"), None)
            continue

        if req.response_format == "b64_json":
            with open(out_path, "rb") as image_file:
                b64_data = base64.b64encode(image_file.read()).decode("utf-8")
                data_outputs.append({"b64_json": b64_data})
        else:
            served_url, r2_key = await publish(filename, out_path)
            data_outputs.append({
                "url": served_url,
                "media_id": r.get("media_id")
            })
            await append_to_history("image", served_url, req.prompt, r.get("media_id"), r2_key)

    return {
        "created": timestamp,
        "data": data_outputs
    }


@app.post("/v1/videos/generations", dependencies=[Depends(verify_api_key)])
async def openai_generate_video(req: VideoGenerationRequest):
    """Generate videos from a prompt (and optional start image)."""
    active_bridge = await get_active_bridge()
    project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

    # Credit gate: only allow as many videos as the balance can afford.
    requested_n = req.n
    cost_each = CREDITS_PER_VIDEO.get(req.duration, 15)
    try:
        cred_res = await active_bridge.api_request("/v1/credits", body=None, captcha_action=None, method="GET")
        cred_data = cred_res.get("data", cred_res) if isinstance(cred_res, dict) else {}
        balance = int(cred_data.get("credits", 0))
    except Exception:
        balance = None

    if balance is not None:
        affordable = balance // cost_each
        if affordable < 1:
            raise HTTPException(
                status_code=402,
                detail=f"Not enough credits: {balance} left, but a {req.duration}s video costs {cost_each}.",
            )
        if req.n > affordable:
            log.warning(
                "Requested %d videos but only %d affordable (%d credits / %d each); capping to %d.",
                req.n, affordable, balance, cost_each, affordable,
            )
            req.n = affordable

    # Map aspect ratio to Flow's ASPECT string
    from omniflash import ASPECTS
    aspect_key = ASPECTS.get(req.aspect, "VIDEO_ASPECT_RATIO_PORTRAIT")

    from omniflash.generators.common import poll_status, download_video
    image_media_id = req.start_media_id
    temp_img_path = None
    is_video_input = bool(req.is_video)

    # If image_base64 is provided and we don't have start_media_id, upload it first
    if req.image_base64 and not image_media_id:
        b64_data = req.image_base64
        is_video_input = b64_data.startswith("data:video/")

        if "," in b64_data:
            b64_data = b64_data.split(",")[1]

        timestamp = int(time.time())
        if is_video_input:
            temp_img_name = f"i2v_upload_{timestamp}_{uuid.uuid4().hex[:6]}.mp4"
        else:
            temp_img_name = f"i2v_upload_{timestamp}_{uuid.uuid4().hex[:6]}.png"
        temp_img_path = os.path.join(OUTPUT_DIR, temp_img_name)

        try:
            with open(temp_img_path, "wb") as f:
                f.write(base64.b64decode(b64_data))

            if is_video_input:
                from omniflash.upload import upload_video
                upload_res = await upload_video(temp_img_path, project_id, active_bridge)
                image_media_id = upload_res.get("mediaId") or upload_res.get("name") or upload_res.get("id")
                if not image_media_id and isinstance(upload_res.get("media"), dict):
                    image_media_id = upload_res["media"].get("name") or upload_res["media"].get("mediaId")
                if not image_media_id:
                    raise HTTPException(status_code=500, detail="Failed to upload start video reference to Google Flow.")
            else:
                from omniflash.generators.i2v import upload_image
                image_media_id = await upload_image(active_bridge, temp_img_path, project_id)
                if not image_media_id:
                    raise HTTPException(status_code=500, detail="Failed to upload start image to Google Flow.")
        except Exception as e:
            log.exception("Error uploading start asset")
            if temp_img_path and os.path.exists(temp_img_path):
                os.remove(temp_img_path)
            raise HTTPException(status_code=500, detail=f"Asset upload error: {str(e)}")

    try:
        # Submit generation
        if is_video_input and image_media_id:
            from omniflash.generators.v2v import edit_video
            media_ids = await edit_video(active_bridge, req.prompt, aspect_key, project_id, image_media_id, duration=req.duration, ref_media_ids=req.ref_media_ids)
        elif req.ref_media_ids:
            from omniflash.generators.i2v import generate_video_r2v
            media_ids = await generate_video_r2v(active_bridge, req.prompt, aspect_key, project_id, req.ref_media_ids, duration=req.duration, count=req.n)
        elif image_media_id:
            from omniflash.generators.i2v import generate_video_i2v
            media_ids = await generate_video_i2v(active_bridge, req.prompt, aspect_key, project_id, image_media_id, duration=req.duration, count=req.n)
        else:
            from omniflash.generators.t2v import generate_video
            media_ids = await generate_video(active_bridge, req.prompt, aspect_key, project_id, duration=req.duration, count=req.n)
    except Exception as e:
        if temp_img_path and os.path.exists(temp_img_path):
            try:
                os.remove(temp_img_path)
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))

    # Clean up temp upload image immediately since it's uploaded to Google Flow
    if temp_img_path and os.path.exists(temp_img_path):
        try:
            os.remove(temp_img_path)
        except Exception:
            pass

    if not media_ids:
        raise HTTPException(status_code=400, detail="Video generation failed to submit.")

    # Poll for status of all videos and download them in parallel
    data_outputs = []
    timestamp = int(time.time())

    async def poll_and_download(media_id: str, index: int):
        success = await poll_status(active_bridge, media_id, project_id)
        if not success:
            log.error(f"Polling failed for media_id: {media_id}")
            return None

        filename = f"flow_vid_{timestamp}_{uuid.uuid4().hex[:6]}_{index+1}.mp4"
        out_path = os.path.join(OUTPUT_DIR, filename)

        dl_success = await download_video(active_bridge, media_id, out_path)
        if not dl_success:
            log.error(f"Download failed for media_id: {media_id}")
            return None

        served_url, r2_key = await publish(filename, out_path)
        return {"url": served_url, "media_id": media_id, "r2_key": r2_key}

    tasks = [poll_and_download(mid, i) for i, mid in enumerate(media_ids)]
    results = await asyncio.gather(*tasks)

    for r in results:
        if r:
            data_outputs.append({"url": r["url"], "media_id": r.get("media_id")})
            await append_to_history("video", r["url"], req.prompt, r.get("media_id"), r.get("r2_key"))

    if not data_outputs:
        raise HTTPException(status_code=500, detail="Failed to complete video generations or downloads.")

    resp = {
        "created": timestamp,
        "data": data_outputs
    }
    if requested_n != len(data_outputs):
        resp["note"] = (
            f"Requested {requested_n} video(s); generated {len(data_outputs)} "
            f"(each {req.duration}s video costs {cost_each} credits)."
        )
    return resp



# Chat completions spec support for custom IDE models
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = "flow-agent"
    messages: List[ChatMessage]
    temperature: Optional[float] = 1.0
    stream: Optional[bool] = False


async def stream_chat_completion(req: ChatCompletionRequest, content: str):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    timestamp = int(time.time())

    # Send assistant role chunk
    yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': timestamp, 'model': req.model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"
    await asyncio.sleep(0.02)

    # Send generated response chunk
    yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': timestamp, 'model': req.model, 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]})}\n\n"
    await asyncio.sleep(0.02)

    # Send stop signal
    yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': timestamp, 'model': req.model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(req: ChatCompletionRequest):
    """Expose image & video generation through standard chat endpoint for editors."""
    # Find last user prompt
    prompt = ""
    for msg in reversed(req.messages):
        if msg.role == "user":
            prompt = msg.content
            break

    if not prompt:
        raise HTTPException(status_code=400, detail="No user message found in the chat history.")

    # Check for short test/connection check prompts from IDEs (like 'hi', 'hello', 'test')
    is_test_prompt = len(prompt.strip()) < 5 or prompt.strip().lower() in ("hello", "test", "ping", "say hi", "hey", "hi there", "testing")

    if is_test_prompt:
        log.info(f"Test/Greeting prompt detected -> '{prompt}'. Returning mock response for verification.")
        markdown_response = "Hello! I am Flow-Agent. I am successfully connected and ready to generate images or videos for you."
    else:
        active_bridge = await get_active_bridge()
        project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

        # Detect video keywords in prompt
        is_video = any(kw in prompt.lower() for kw in ["video", "animate", "generate video", "make video", "mp4"])

        if is_video:
            # Import video generator on demand
            from omniflash.generators.t2v import generate_video
            from omniflash.generators.common import poll_status, download_video
            from omniflash import ASPECTS

            log.info(f"Custom Chat Prompt: Video Generation -> '{prompt}'")
            aspect_key = ASPECTS.get("portrait", "VIDEO_ASPECT_RATIO_PORTRAIT")
            media_ids = await generate_video(active_bridge, prompt, aspect_key, project_id)
            if not media_ids:
                raise HTTPException(status_code=500, detail="Failed to initiate video generation.")

            media_id = media_ids[0]
            if not await poll_status(active_bridge, media_id, project_id):
                raise HTTPException(status_code=500, detail="Video generation failed during polling.")

            timestamp = int(time.time())
            filename = f"openai_chat_vid_{timestamp}_{uuid.uuid4().hex[:6]}_1.mp4"
            out_path = os.path.join(OUTPUT_DIR, filename)

            if not await download_video(active_bridge, media_id, out_path):
                raise HTTPException(status_code=500, detail="Failed to download video file.")

            download_url, _ = await publish(filename, out_path)
            markdown_response = f"**Video Generated successfully!**\n\n[Download / Play Video]({download_url})\n\n"
        else:
            log.info(f"Custom Chat Prompt: Image Generation -> '{prompt}'")
            results = await generate_image(active_bridge, prompt=prompt, aspect="square", project_id=project_id, count=1)
            if not results or not results[0].get("image_url"):
                raise HTTPException(status_code=500, detail="Flow failed to generate image.")

            url = results[0]["image_url"]
            timestamp = int(time.time())
            filename = f"openai_chat_img_{timestamp}_{uuid.uuid4().hex[:6]}_1.png"
            out_path = os.path.join(OUTPUT_DIR, filename)

            if not await download_image(active_bridge, url, out_path):
                raise HTTPException(status_code=500, detail="Failed to download image file.")

            download_url, _ = await publish(filename, out_path)
            markdown_response = f" **Image Generated successfully!**\n\n![Generated Image]({download_url})\n\n"

    # Support streaming mode
    if req.stream:
        return StreamingResponse(
            stream_chat_completion(req, markdown_response),
            media_type="text/event-stream"
        )

    # Return standard non-streaming response
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": markdown_response
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": len(prompt) // 4,
            "completion_tokens": len(markdown_response) // 4,
            "total_tokens": (len(prompt) + len(markdown_response)) // 4
        }
    }


class UploadRequest(BaseModel):
    image_base64: str = Field(..., description="Base64 encoded image or video data")

@app.post("/v1/upload", dependencies=[Depends(verify_api_key)])
async def upload_file_endpoint(req: UploadRequest):
    """Upload a file (image or video) to Google Flow and return its media ID and local URL."""
    active_bridge = await get_active_bridge()
    project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

    b64_data = req.image_base64
    is_video_input = b64_data.startswith("data:video/")

    if "," in b64_data:
        b64_data = b64_data.split(",")[1]

    timestamp = int(time.time())
    if is_video_input:
        temp_name = f"upload_{timestamp}_{uuid.uuid4().hex[:6]}.mp4"
    else:
        temp_name = f"upload_{timestamp}_{uuid.uuid4().hex[:6]}.png"
    temp_path = os.path.join(OUTPUT_DIR, temp_name)

    try:
        with open(temp_path, "wb") as f:
            f.write(base64.b64decode(b64_data))

        if is_video_input:
            from omniflash.upload import upload_video
            upload_res = await upload_video(temp_path, project_id, active_bridge)
            media_id = upload_res.get("mediaId") or upload_res.get("name") or upload_res.get("id")
            if not media_id and isinstance(upload_res.get("media"), dict):
                media_id = upload_res["media"].get("name") or upload_res["media"].get("mediaId")
            if not media_id:
                raise HTTPException(status_code=500, detail="Failed to upload video reference to Google Flow.")
        else:
            from omniflash.generators.i2v import upload_image
            media_id = await upload_image(active_bridge, temp_path, project_id)
            if not media_id:
                raise HTTPException(status_code=500, detail="Failed to upload image reference to Google Flow.")

        # Make the file web-accessible (R2 if configured, else local /download)
        download_url, r2_key = await publish(temp_name, temp_path)
        # If it went to R2, the local copy is no longer needed for serving
        if r2_key and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

        # Add to history
        await append_to_history("video" if is_video_input else "image", download_url, "Uploaded reference file", media_id, r2_key)

        return {
            "media_id": media_id,
            "url": download_url
        }
    except Exception as e:
        log.exception("Error in /v1/upload")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))


# History Management Helper
async def append_to_history(type_str: str, url: str, prompt: str, media_id: str = None, r2_key: str = None):
    """Record a generation. Uses history.json."""
    _append_history_file(type_str, url, prompt, media_id)


def _append_history_file(type_str: str, url: str, prompt: str, media_id: str = None):
    history_file = os.path.join(OUTPUT_DIR, "history.json")
    data = {"history": []}
    if os.path.exists(history_file):
        try:
            with open(history_file, "r") as f:
                data = json.load(f)
        except Exception:
            pass
    data["history"].insert(0, {
        "type": type_str,
        "url": url,
        "prompt": prompt,
        "timestamp": int(time.time()),
        "media_id": media_id
    })
    # Cap history at 100 entries to avoid massive files
    data["history"] = data["history"][:100]
    try:
        with open(history_file, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


@app.get("/v1/history")
async def get_history():
    """Get previously generated images and videos."""
    history_file = os.path.join(OUTPUT_DIR, "history.json")
    if not os.path.exists(history_file):
        # Auto-detect existing generated files to populate initial history
        history_list = []
        try:
            files = sorted(
                [f for f in os.listdir(OUTPUT_DIR) if f.startswith(("openai_img_", "flowagent_img_", "flow_vid_", "openai_chat_vid_"))],
                key=lambda f: os.path.getmtime(os.path.join(OUTPUT_DIR, f)),
                reverse=True
            )
            for filename in files[:100]:
                file_path = os.path.join(OUTPUT_DIR, filename)
                t = int(os.path.getmtime(file_path))
                is_vid = filename.endswith(".mp4")
                download_url = public_url(filename)
                history_list.append({
                    "type": "video" if is_vid else "image",
                    "url": download_url,
                    "prompt": "Pre-existing generation" if not filename.startswith("openai_chat_") else "Chat video prompt",
                    "timestamp": t,
                    "media_id": None
                })
            # Save it
            with open(history_file, "w") as f:
                json.dump({"history": history_list}, f, indent=2)
            return {"history": history_list}
        except Exception:
            return {"history": []}

    try:
        with open(history_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"history": []}


@app.delete("/v1/history")
async def delete_all_history():
    """Clear all generation history and delete files."""
    history_file = os.path.join(OUTPUT_DIR, "history.json")
    try:
        if os.path.exists(history_file):
            os.remove(history_file)

        # Remove all generated and uploaded output files
        for filename in os.listdir(OUTPUT_DIR):
            if filename.startswith(("openai_img_", "flowagent_img_", "flow_vid_", "openai_chat_vid_", "openai_chat_img_", "upload_", "i2i_upload_", "i2v_upload_")):
                file_path = os.path.join(OUTPUT_DIR, filename)
                if os.path.exists(file_path):
                    os.remove(file_path)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear output folder: {str(e)}")


@app.delete("/v1/history/{filename}")
async def delete_history_item(filename: str):
    """Delete a single history item and its corresponding file."""
    history_file = os.path.join(OUTPUT_DIR, "history.json")

    # Delete from disk
    file_path = os.path.join(OUTPUT_DIR, filename)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            log.error(f"Failed to delete file {file_path}: {e}")

    # Delete from history.json metadata
    if os.path.exists(history_file):
        try:
            with open(history_file, "r") as f:
                data = json.load(f)

            initial_len = len(data.get("history", []))
            # Filter out items whose URL contains this filename
            data["history"] = [
                item for item in data.get("history", [])
                if filename not in item["url"]
            ]

            with open(history_file, "w") as f:
                json.dump(data, f, indent=2)

            return {"status": "success", "deleted": initial_len - len(data["history"])}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update history data: {str(e)}")

    return {"status": "success", "info": "metadata file not found"}


@app.get("/download/{filename}")
async def download_file(filename: str):
    """Serve the generated assets."""
    file_path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    media_type = "image/png"
    if filename.endswith(".mp4"):
        media_type = "video/mp4"
    return FileResponse(path=file_path, media_type=media_type)


@app.get("/v1/credits", dependencies=[Depends(verify_api_key)])
async def get_flow_credits():
    active_bridge = await get_active_bridge()
    try:
        res = await active_bridge.api_request("/v1/credits", body=None, captcha_action=None, method="GET")
        if isinstance(res, dict):
            if res.get("status", 200) != 200:
                raise HTTPException(status_code=res.get("status", 500), detail=str(res.get("error", "API error")))
            if "data" in res:
                return res["data"]
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    return {"status": "running", "service": "Flow Agent API"}


# Health Check
@app.get("/health")
async def health():
    global bridge
    if not bridge:
        return {"status": "starting", "connected": False}
    return {
        "status": "healthy" if await bridge.health_check() else "unauthorized_or_disconnected",
        "extension_connected": bridge._ws is not None,
        "has_flow_key": bridge._flow_key is not None
    }


# --- MCP SSE Server Implementation ---

SSE_CLIENTS = {}

@app.get("/sse")
async def sse_endpoint(request: Request):
    session_id = str(uuid.uuid4())
    queue = asyncio.Queue()
    SSE_CLIENTS[session_id] = queue
    log.info(f"[MCP SSE] Client session created: {session_id}")

    # Get request host and scheme, taking proxy headers into account
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    base_url = f"{scheme}://{host}"
    endpoint_url = f"{base_url}/messages?session_id={session_id}"
    log.info(f"[MCP SSE] Exposing endpoint URL: {endpoint_url}")

    async def event_generator():
        try:
            # According to the MCP SSE spec, we must send an "endpoint" event with the message URI
            yield f"event: endpoint\ndata: {endpoint_url}\n\n"

            while True:
                message = await queue.get()
                yield f"event: message\ndata: {json.dumps(message)}\n\n"
        except asyncio.CancelledError:
            log.info(f"[MCP SSE] Client session cancelled: {session_id}")
        finally:
            if session_id in SSE_CLIENTS:
                del SSE_CLIENTS[session_id]
                log.info(f"[MCP SSE] Client session cleaned up: {session_id}")

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/messages")
async def messages_endpoint(request: Request, session_id: str = Query(...)):
    if session_id not in SSE_CLIENTS:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    try:
        body_bytes = await request.body()
        if not body_bytes:
            raise HTTPException(status_code=400, detail="Empty request body")
        req_data = json.loads(body_bytes.decode("utf-8"))
        log.info(f"[MCP SSE] Received request: {req_data}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    method = req_data.get("method")
    request_id = req_data.get("id")

    response = None
    if method == "initialize":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "flow-mcp",
                    "version": "1.0.0"
                }
            }
        }
    elif method == "initialized":
        # MCP initialization notification
        return JSONResponse({"ok": True})
    elif method == "tools/list":
        tools = get_mcp_tools_list()
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": tools
            }
        }
    elif method == "tools/call":
        params = req_data.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        response = await execute_mcp_tool(request_id, tool_name, arguments)
    elif method == "ping":
        response = {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if response:
        await SSE_CLIENTS[session_id].put(response)

    return JSONResponse({"ok": True})


def get_mcp_tools_list():
    return [
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
                    "ref_media_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of pre-uploaded media IDs (e.g. from upload_flow_media) to use as reference for Image-to-Image"
                    }
                },
                "required": ["prompt"]
            }
        },
        {
            "name": "generate_flow_video",
            "description": "Generate a 10-second cinematic video clip using Google Flow. Optionally supports a starting frame reference image (Image-to-Video).",
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
                    "start_media_id": {
                        "type": "string",
                        "description": "Optional pre-uploaded media ID (e.g. from upload_flow_media) to use as start frame for Image-to-Video"
                    }
                },
                "required": ["prompt"]
            }
        },
        {
            "name": "upload_flow_media",
            "description": "Upload a reference image or video to Google Flow using a public URL, base64 data, or a local file path, and return its media_id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "image_url": {
                        "type": "string",
                        "description": "Optional public HTTPS URL of the image/video to download and upload."
                    },
                    "image_base64": {
                        "type": "string",
                        "description": "Optional Base64 encoded image string to upload."
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional local file path of the image/video to upload."
                    }
                }
            }
        }
    ]


async def execute_mcp_tool(request_id, tool_name, arguments):
    text = ""
    image_data_b64 = None

    try:
        if tool_name == "get_flow_credits":
            active_bridge = await get_active_bridge()
            res = await active_bridge.api_request("/v1/credits", body=None, captcha_action=None, method="GET")
            if isinstance(res, dict) and "data" in res:
                credits = res["data"].get("credits", "unknown")
            elif isinstance(res, dict) and "credits" in res:
                credits = res.get("credits", "unknown")
            else:
                credits = "unknown"
            text = f"Remaining Google Flow credits/generations: {credits}"

        elif tool_name == "generate_flow_image":
            prompt = arguments.get("prompt")
            size = arguments.get("size", "1280x720")
            ref_image_path = arguments.get("ref_image_path")

            ref_image_base64 = None
            if ref_image_path:
                if not os.path.exists(ref_image_path):
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": f"Error: Reference image path does not exist: {ref_image_path}"
                        }
                    }
                try:
                    with open(ref_image_path, "rb") as f:
                        ref_image_base64 = base64.b64encode(f.read()).decode("utf-8")
                except Exception as e:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": f"Error reading reference image: {str(e)}"
                        }
                    }

            ref_media_ids = arguments.get("ref_media_ids")
            req = ImageGenerationRequest(
                prompt=prompt,
                size=size,
                n=1,
                image_base64=ref_image_base64,
                ref_media_ids=ref_media_ids
            )

            res = await openai_generate_image(req)
            data = res.get("data", [])
            if not data:
                text = "No images returned by Flow Agent."
            else:
                img_url = data[0]["url"]
                text = f"Success! Image generated successfully.\nURL: {img_url}"

                # Read local image directly to base64
                try:
                    filename = img_url.split("/")[-1]
                    local_path = os.path.join(OUTPUT_DIR, filename)
                    if os.path.exists(local_path):
                        with open(local_path, "rb") as f:
                            image_data_b64 = base64.b64encode(f.read()).decode("utf-8")
                except Exception as e:
                    log.error(f"Failed to read local image for base64: {e}")

        elif tool_name == "generate_flow_video":
            prompt = arguments.get("prompt")
            aspect = arguments.get("aspect", "landscape")
            start_image_path = arguments.get("start_image_path")
            start_media_id = arguments.get("start_media_id")

            start_image_base64 = None
            if start_image_path:
                if not os.path.exists(start_image_path):
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": f"Error: Starting image path does not exist: {start_image_path}"
                        }
                    }
                try:
                    with open(start_image_path, "rb") as f:
                        start_image_base64 = base64.b64encode(f.read()).decode("utf-8")
                except Exception as e:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": f"Error reading starting image: {str(e)}"
                        }
                    }

            req = VideoGenerationRequest(
                prompt=prompt,
                aspect=aspect,
                n=1,
                duration=10,
                image_base64=start_image_base64,
                start_media_id=start_media_id
            )

            res = await openai_generate_video(req)
            data = res.get("data", [])
            if not data:
                text = "No videos returned by Flow Agent."
            else:
                vid_url = data[0]["url"]
                text = (
                    f"### Google Flow Video Generated!\n\n"
                    f"**Prompt:** *{prompt}*\n\n"
                    f"[Click Here to Watch / Download Video]({vid_url})"
                )
                if start_image_base64:
                    image_data_b64 = start_image_base64
        elif tool_name == "upload_flow_media":
            image_url = arguments.get("image_url")
            image_base64 = arguments.get("image_base64")
            file_path = arguments.get("file_path")

            active_bridge = await get_active_bridge()
            project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

            if not image_url and not image_base64 and not file_path:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32602,
                        "message": "Error: You must provide at least one of 'image_url', 'image_base64', or 'file_path'."
                    }
                }

            timestamp = int(time.time())
            unique_id = uuid.uuid4().hex[:6]

            temp_filename = f"mcp_upload_{timestamp}_{unique_id}.png"
            temp_path = os.path.join(OUTPUT_DIR, temp_filename)
            upload_path = None
            is_temp = False

            try:
                if file_path:
                    if not os.path.exists(file_path):
                        raise Exception(f"File path does not exist: {file_path}")
                    upload_path = file_path
                elif image_base64:
                    if "," in image_base64:
                        image_base64 = image_base64.split(",")[1]
                    with open(temp_path, "wb") as f:
                        f.write(base64.b64decode(image_base64))
                    upload_path = temp_path
                    is_temp = True
                elif image_url:
                    ext = ".png"
                    if ".mp4" in image_url.lower():
                        ext = ".mp4"
                    elif ".jpg" in image_url.lower() or ".jpeg" in image_url.lower():
                        ext = ".jpg"

                    if ext != ".png":
                        temp_path = os.path.join(OUTPUT_DIR, f"mcp_upload_{timestamp}_{unique_id}{ext}")

                    req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=30) as response:
                        with open(temp_path, "wb") as f:
                            f.write(response.read())
                    upload_path = temp_path
                    is_temp = True

                is_video = upload_path.lower().endswith(".mp4")
                if is_video:
                    from omniflash.upload import upload_video
                    upload_res = await upload_video(upload_path, project_id, active_bridge)
                    media_id = upload_res.get("mediaId") or upload_res.get("name") or upload_res.get("id")
                    if not media_id and isinstance(upload_res.get("media"), dict):
                        media_id = upload_res["media"].get("name") or upload_res["media"].get("mediaId")
                else:
                    from omniflash.generators.i2v import upload_image
                    media_id = await upload_image(active_bridge, upload_path, project_id)

                if is_temp and os.path.exists(upload_path):
                    os.remove(upload_path)

                if not media_id:
                    raise Exception("Failed to upload asset to Google Flow.")

                text = f"Successfully uploaded asset to Google Flow.\nmedia_id: {media_id}"
            except Exception as e:
                log.exception("Failed to upload media")
                if is_temp and upload_path and os.path.exists(upload_path):
                    os.remove(upload_path)
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32603,
                        "message": f"Upload failed: {str(e)}"
                    }
                }
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {tool_name}"
                }
            }
    except Exception as e:
        log.exception(f"Exception executing tool {tool_name}")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32603,
                "message": f"Tool execution failed: {str(e)}"
            }
        }

    content = [{"type": "text", "text": text}]
    if image_data_b64:
        content.append({
            "type": "image",
            "data": image_data_b64,
            "mimeType": "image/png"
        })

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": content
        }
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Flow Agent OpenAI API Server")
    parser.add_argument("--host", default=os.environ.get("OPENAI_API_HOST", "127.0.0.1"), help="Host address")
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPENAI_API_PORT", "8001")), help="Port to run on")
    args = parser.parse_args()

    import uvicorn
    uvicorn.run("cli.api:app", host=args.host, port=args.port)
