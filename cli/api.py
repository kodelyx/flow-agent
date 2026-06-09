#!/usr/bin/env python3
"""FastAPI Server for Flow Agent — expose CLI functionality via HTTP/HTTPS.

Allows n8n and other remote systems to trigger video/image generation and upload assets.
"""

import os
import sys
import uuid
import time
import shutil
import base64
import logging
import asyncio
from typing import List, Optional
from contextlib import asynccontextmanager

# Add parent dir to sys.path so omniflash can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from omniflash import (
    ExtensionBridge, generate_video, edit_video,
    poll_status, download_video, ASPECTS, DEFAULT_PROJECT,
)
from omniflash.generators.i2v import upload_image, generate_video_i2v, generate_video_fl, generate_video_r2v
from omniflash.generators.t2i import generate_image, download_image, IMAGE_ASPECTS
from omniflash.upload import upload_video

# Setup logging
log = logging.getLogger("omniflash.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

# Ensure required directories exist
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
TEMP_DIR = os.path.join(OUTPUT_DIR, ".temp")

def ensure_temp_dir():
    os.makedirs(TEMP_DIR, exist_ok=True)

def cleanup_temp_dir():
    try:
        if os.path.exists(TEMP_DIR) and not os.listdir(TEMP_DIR):
            os.rmdir(TEMP_DIR)
    except Exception:
        pass

# Global ExtensionBridge instance
bridge: Optional[ExtensionBridge] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge
    log.info("🚀 Starting Flow Agent Extension Bridge...")
    bridge = ExtensionBridge()
    await bridge.start()
    
    # Run extension connection in background so the API server starts immediately
    asyncio.create_task(bridge.wait_for_extension(timeout=30))
    
    yield
    
    log.info("🔌 Closing Flow Agent Extension Bridge...")
    if bridge:
        await bridge.close()
    cleanup_temp_dir()

app = FastAPI(
    title="Flow Agent API",
    description="API Server to trigger Google Flow AI video and image generation",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS for convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Helper function to check/reconnect the bridge
async def get_active_bridge() -> ExtensionBridge:
    global bridge
    if not bridge:
        raise HTTPException(status_code=503, detail="Extension bridge is not initialized")
    
    # Try a quick health check
    is_healthy = await bridge.health_check()
    if not is_healthy:
        log.info("🔄 Bridge health check failed. Re-waiting for extension connection...")
        # Attempt to reconnect / grab flowKey
        connected = await bridge.wait_for_extension(timeout=10, max_retries=1)
        if not connected:
            raise HTTPException(
                status_code=503,
                detail="Google Flow extension is not connected or unauthorized. Make sure Google Flow tab is open in Chrome."
            )
    return bridge

# Helper to process image inputs (local path, media_id, or base64 data)
async def resolve_image_input(active_bridge: ExtensionBridge, path_or_id_or_b64: str, project_id: str) -> str:
    if not path_or_id_or_b64:
        return ""
    
    # Case 1: Base64 data (e.g. data:image/png;base64,... or raw base64)
    if path_or_id_or_b64.startswith("data:") or len(path_or_id_or_b64) > 500:
        try:
            if "," in path_or_id_or_b64:
                base64_data = path_or_id_or_b64.split(",", 1)[1]
            else:
                base64_data = path_or_id_or_b64
            
            img_bytes = base64.b64decode(base64_data)
            temp_filename = f"b64_{uuid.uuid4().hex}.png"
            ensure_temp_dir()
            temp_path = os.path.join(TEMP_DIR, temp_filename)
            with open(temp_path, "wb") as f:
                f.write(img_bytes)
            
            mid = await upload_image(active_bridge, temp_path, project_id)
            try:
                os.remove(temp_path)
            except OSError:
                pass
            cleanup_temp_dir()
            
            if not mid:
                raise HTTPException(status_code=400, detail="Failed to upload base64 image reference")
            return mid
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed parsing base64 image: {str(e)}")
            
    # Case 2: Local file path
    if os.path.exists(path_or_id_or_b64):
        mid = await upload_image(active_bridge, path_or_id_or_b64, project_id)
        if not mid:
            raise HTTPException(status_code=400, detail=f"Failed to upload local image path: {path_or_id_or_b64}")
        return mid
        
    # Case 3: Already a Media ID (UUID or similar format)
    return path_or_id_or_b64


# Request Models
class VideoGenerationRequest(BaseModel):
    prompt: str = Field(..., description="Text prompt for video generation")
    aspect: str = Field("portrait", description="Aspect ratio: 'portrait' or 'landscape'")
    duration: int = Field(10, description="Duration in seconds: 4, 6, 8, or 10")
    count: int = Field(1, description="Number of variations (1-4)")
    project_id: str = Field(DEFAULT_PROJECT, description="Flow project ID")
    start: Optional[str] = Field(None, description="Start frame image (file path, media_id, or base64)")
    end: Optional[str] = Field(None, description="End frame image (use with start for FL mode)")
    ref: Optional[List[str]] = Field(None, description="Reference image(s) (file path, media_id, or base64)")
    edit: Optional[str] = Field(None, description="Flow video media_id for video editing (V2V)")
    no_clean: bool = Field(False, description="Skip watermark removal")


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., description="Text prompt for image generation")
    aspect: str = Field("portrait", description="Aspect ratio: 'portrait', 'landscape', 'square', '4x3', '3x4'")
    count: int = Field(1, description="Number of variations (1-4)")
    ref: Optional[List[str]] = Field(None, description="Reference image(s) (file path, media_id, or base64)")
    project_id: str = Field(DEFAULT_PROJECT, description="Flow project ID")


class VideoEditRequest(BaseModel):
    prompt: str = Field(..., description="Restyle/edit text prompt")
    video_media_id: str = Field(..., description="Original video media_id")
    aspect: str = Field("portrait", description="Aspect ratio: 'portrait' or 'landscape'")
    fps: int = Field(24, description="FPS of source video")
    duration: int = Field(10, description="Duration of segment to edit")
    start_frame: int = Field(0, description="Start frame index")
    end_frame: Optional[int] = Field(None, description="End frame index")
    project_id: str = Field(DEFAULT_PROJECT, description="Flow project ID")
    download: bool = Field(False, description="Directly download binary video stream")


# API Routes

@app.get("/health")
async def health():
    """Check API server connection and Chrome extension authorization."""
    global bridge
    if not bridge:
        return {"status": "starting", "extension_connected": False, "has_flow_key": False}
    
    is_healthy = await bridge.health_check()
    return {
        "status": "healthy" if is_healthy else "disconnected_or_unauthorized",
        "extension_connected": bridge._ws is not None,
        "has_flow_key": bridge._flow_key is not None
    }


@app.post("/upload/image")
async def api_upload_image(
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = Form(None),
    project_id: str = Form(DEFAULT_PROJECT)
):
    """Upload an image to Google Flow. Accepts multipart file upload or local file path."""
    active_bridge = await get_active_bridge()
    
    temp_path = None
    if file:
        temp_filename = f"upload_{uuid.uuid4().hex}_{file.filename}"
        ensure_temp_dir()
        temp_path = os.path.join(TEMP_DIR, temp_filename)
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        upload_path = temp_path
    elif path:
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail=f"Local file not found: {path}")
        upload_path = path
    else:
        raise HTTPException(status_code=400, detail="Must provide 'file' (multipart) or 'path' (form parameter)")
    
    try:
        media_id = await upload_image(active_bridge, upload_path, project_id)
        if not media_id:
            raise HTTPException(status_code=500, detail="Flow image upload failed")
        return {"success": True, "media_id": media_id}
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        cleanup_temp_dir()


@app.post("/upload/video")
async def api_upload_video(
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = Form(None),
    project_id: str = Form(DEFAULT_PROJECT)
):
    """Upload a video to Google Flow. Accepts multipart file upload or local file path."""
    active_bridge = await get_active_bridge()
    
    temp_path = None
    if file:
        temp_filename = f"upload_{uuid.uuid4().hex}_{file.filename}"
        ensure_temp_dir()
        temp_path = os.path.join(TEMP_DIR, temp_filename)
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        upload_path = temp_path
    elif path:
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail=f"Local file not found: {path}")
        upload_path = path
    else:
        raise HTTPException(status_code=400, detail="Must provide 'file' (multipart) or 'path' (form parameter)")
    
    try:
        result = await upload_video(upload_path, project_id, active_bridge)
        media_id = result.get("mediaId") or result.get("name") or result.get("id")
        if not media_id and isinstance(result.get("media"), dict):
            media_id = result["media"].get("name") or result["media"].get("mediaId")
            
        if not media_id:
            raise HTTPException(status_code=500, detail=f"Flow video upload failed: {result}")
        return {"success": True, "media_id": media_id, "data": result}
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        cleanup_temp_dir()


@app.post("/generate/video")
async def api_generate_video(req: VideoGenerationRequest, download: bool = Query(False)):
    """Generate or edit video via text prompt and optional references (T2V, I2V, FL, R2V, V2V)."""
    active_bridge = await get_active_bridge()
    aspect = ASPECTS.get(req.aspect, "VIDEO_ASPECT_RATIO_PORTRAIT")
    
    # 1. Resolve starting image (I2V / FL)
    start_id = None
    if req.start:
        start_id = await resolve_image_input(active_bridge, req.start, req.project_id)
        
    # 2. Resolve end image (FL)
    end_id = None
    if req.end:
        end_id = await resolve_image_input(active_bridge, req.end, req.project_id)
        
    # 3. Resolve reference images (R2V)
    ref_ids = []
    if req.ref:
        for r in req.ref:
            mid = await resolve_image_input(active_bridge, r, req.project_id)
            if mid:
                ref_ids.append(mid)
                
    # 4. Trigger generation
    media_ids = None
    if start_id and end_id:
        media_ids = await generate_video_fl(
            active_bridge, req.prompt, aspect, req.project_id,
            start_image_id=start_id, end_image_id=end_id, duration=req.duration
        )
    elif start_id:
        media_ids = await generate_video_i2v(
            active_bridge, req.prompt, aspect, req.project_id,
            image_media_id=start_id, duration=req.duration
        )
    elif ref_ids:
        media_ids = await generate_video_r2v(
            active_bridge, req.prompt, aspect, req.project_id,
            ref_media_ids=ref_ids, duration=req.duration
        )
    elif req.edit:
        media_ids = await edit_video(
            active_bridge, req.prompt, aspect, req.project_id,
            video_media_id=req.edit, duration=req.duration
        )
    else:
        media_ids = await generate_video(
            active_bridge, req.prompt, aspect, req.project_id,
            duration=req.duration, count=req.count
        )
        
    if not media_ids:
        raise HTTPException(status_code=500, detail="Failed to initiate video generation")
        
    outputs = []
    timestamp = int(time.time())
    
    # 5. Poll and Download
    for i, media_id in enumerate(media_ids):
        log.info(f"Polling video [{i+1}/{len(media_ids)}] ID: {media_id}")
        if not await poll_status(active_bridge, media_id, req.project_id):
            log.error(f"Polling failed for media ID: {media_id}")
            continue
            
        unique_id = uuid.uuid4().hex[:6]
        filename = f"omni_{timestamp}_{unique_id}_{i+1}.mp4"
        out_path = os.path.join(OUTPUT_DIR, filename)
        ensure_temp_dir()
        temp_path = os.path.join(TEMP_DIR, filename)
        
        if await download_video(active_bridge, media_id, temp_path):
            if not req.no_clean:
                try:
                    from omniflash.watermark import remove_watermark_video
                    remove_watermark_video(temp_path, out_path)
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                except Exception as e:
                    log.warning(f"Watermark removal failed: {e}. Fallback to raw video.")
                    os.replace(temp_path, out_path)
            else:
                os.replace(temp_path, out_path)
                
            outputs.append({
                "media_id": media_id,
                "filename": filename,
                "local_path": out_path,
                "download_url": f"/download/{filename}"
            })
            
    cleanup_temp_dir()
    if not outputs:
        raise HTTPException(status_code=500, detail="Failed to download generated video(s)")
        
    # Return binary directly if requested and single file
    if download and len(outputs) == 1:
        return FileResponse(
            path=outputs[0]["local_path"],
            filename=outputs[0]["filename"],
            media_type="video/mp4"
        )
        
    return {
        "success": True,
        "outputs": outputs
    }


@app.post("/generate/image")
async def api_generate_image(req: ImageGenerationRequest, download: bool = Query(False)):
    """Generate image using text prompt and optional reference images (T2I, I2I)."""
    active_bridge = await get_active_bridge()
    aspect = req.aspect
    
    # Resolve reference images if any
    ref_ids = []
    if req.ref:
        for r in req.ref:
            mid = await resolve_image_input(active_bridge, r, req.project_id)
            if mid:
                ref_ids.append(mid)
                
    results = await generate_image(
        active_bridge, req.prompt, aspect, req.project_id,
        count=req.count, ref_media_ids=ref_ids or None
    )
    
    if not results:
        raise HTTPException(status_code=500, detail="Failed to generate image")
        
    outputs = []
    timestamp = int(time.time())
    
    for i, r in enumerate(results):
        url = r.get("image_url")
        media_id = r.get("media_id")
        if not url:
            continue
            
        unique_id = uuid.uuid4().hex[:6]
        filename = f"img_{timestamp}_{unique_id}_{i+1}.png"
        out_path = os.path.join(OUTPUT_DIR, filename)
        
        download_success = await download_image(active_bridge, url, out_path)
        
        outputs.append({
            "media_id": media_id,
            "filename": filename,
            "local_path": out_path if download_success else None,
            "download_url": f"/download/{filename}" if download_success else None,
            "remote_url": url,
            "downloaded": download_success
        })
        
    # Return binary directly if requested, single image, and it was successfully downloaded
    if download and len(outputs) == 1 and outputs[0]["downloaded"]:
        return FileResponse(
            path=outputs[0]["local_path"],
            filename=outputs[0]["filename"],
            media_type="image/png"
        )
        
    return {
        "success": True,
        "outputs": outputs
    }


@app.post("/edit/video")
async def api_edit_video(req: VideoEditRequest):
    """Submit V2V edit request."""
    active_bridge = await get_active_bridge()
    aspect = ASPECTS.get(req.aspect, "VIDEO_ASPECT_RATIO_PORTRAIT")
    
    media_ids = await edit_video(
        active_bridge, req.prompt, aspect, req.project_id,
        video_media_id=req.video_media_id, fps=req.fps,
        duration=req.duration, start_frame=req.start_frame,
        end_frame=req.end_frame
    )
    
    if not media_ids:
        raise HTTPException(status_code=500, detail="Failed to submit V2V edit request")
        
    outputs = []
    timestamp = int(time.time())
    
    for i, media_id in enumerate(media_ids):
        log.info(f"Polling edited video [{i+1}/{len(media_ids)}] ID: {media_id}")
        if not await poll_status(active_bridge, media_id, req.project_id):
            continue
            
        unique_id = uuid.uuid4().hex[:6]
        filename = f"edit_{timestamp}_{unique_id}_{i+1}.mp4"
        out_path = os.path.join(OUTPUT_DIR, filename)
        ensure_temp_dir()
        temp_path = os.path.join(TEMP_DIR, filename)
        
        if await download_video(active_bridge, media_id, temp_path):
            # V2V edited segments might also have watermarks
            try:
                from omniflash.watermark import remove_watermark_video
                remove_watermark_video(temp_path, out_path)
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            except Exception as e:
                log.warning(f"Watermark removal failed: {e}. Fallback to raw video.")
                os.replace(temp_path, out_path)
                
            outputs.append({
                "media_id": media_id,
                "filename": filename,
                "local_path": out_path,
                "download_url": f"/download/{filename}"
            })
            
    cleanup_temp_dir()
    if not outputs:
        raise HTTPException(status_code=500, detail="Failed to download edited video(s)")
        
    if req.download and len(outputs) == 1:
        return FileResponse(
            path=outputs[0]["local_path"],
            filename=outputs[0]["filename"],
            media_type="video/mp4"
        )
        
    return {
        "success": True,
        "outputs": outputs
    }


@app.get("/download/{filename}")
async def api_download_file(filename: str):
    """Download generated assets from output folder."""
    file_path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Requested file not found")
        
    # Standardize content types
    media_type = "application/octet-stream"
    if filename.endswith(".mp4"):
        media_type = "video/mp4"
    elif filename.endswith(".png"):
        media_type = "image/png"
    elif filename.endswith(".jpg") or filename.endswith(".jpeg"):
        media_type = "image/jpeg"
        
    return FileResponse(path=file_path, filename=filename, media_type=media_type)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Flow Agent API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host address")
    parser.add_argument("--port", type=int, default=8000, help="Port to run on")
    parser.add_argument("--ssl", action="store_true", help="Enable self-signed SSL certificate")
    parser.add_argument("--ssl-certfile", help="SSL certificate file path")
    parser.add_argument("--ssl-keyfile", help="SSL private key file path")
    args = parser.parse_args()

    ssl_keyfile = args.ssl_keyfile
    ssl_certfile = args.ssl_certfile

    if args.ssl and not (ssl_keyfile and ssl_certfile):
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.primitives import serialization
            import datetime

            # Generate RSA key
            key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
            
            # Create self-signed cert info
            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, u"localhost"),
            ])
            cert = x509.CertificateBuilder().subject_name(
                subject
            ).issuer_name(
                issuer
            ).public_key(
                key.public_key()
            ).serial_number(
                x509.random_serial_number()
            ).not_valid_before(
                datetime.datetime.utcnow()
            ).not_valid_after(
                datetime.datetime.utcnow() + datetime.timedelta(days=365)
            ).add_extension(
                x509.SubjectAlternativeName([x509.DNSName(u"localhost")]),
                critical=False,
            ).sign(key, hashes.SHA256())

            ssl_dir = os.path.join(OUTPUT_DIR, ".ssl")
            os.makedirs(ssl_dir, exist_ok=True)
            ssl_keyfile = os.path.join(ssl_dir, "key.pem")
            ssl_certfile = os.path.join(ssl_dir, "cert.pem")

            with open(ssl_keyfile, "wb") as f:
                f.write(key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                ))
            with open(ssl_certfile, "wb") as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM))

            log.info(f"🔒 Generated temporary self-signed SSL certificate in {ssl_dir}")
        except ImportError:
            log.warning("⚠️ cryptography package not found. Cannot auto-generate self-signed SSL cert.")
            log.warning("   Please install it: pip install cryptography")
            log.warning("   Falling back to standard HTTP.")
            args.ssl = False

    import uvicorn
    uvicorn.run(
        "cli.api:app",
        host=args.host,
        port=args.port,
        ssl_keyfile=ssl_keyfile if args.ssl or (args.ssl_keyfile and args.ssl_keyfile) else None,
        ssl_certfile=ssl_certfile if args.ssl or (args.ssl_keyfile and args.ssl_keyfile) else None,
    )
