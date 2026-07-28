#!/usr/bin/env python3
"""Media routes for Flow Agent.

Upload, generation history management, and serving of generated assets.
"""

import os
import uuid
import time
import json
import base64
import logging

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse

from flow_engine import DEFAULT_PROJECT

from flow_server.config import OUTPUT_DIR, public_url
from flow_server.models import UploadRequest
from flow_server.state import verify_api_key, get_active_bridge, publish, append_to_history

# Setup logging (format configured centrally in flow_engine/__init__.py, imported above)
log = logging.getLogger("flow_engine.openai_api")

router = APIRouter()


@router.post("/v1/upload", dependencies=[Depends(verify_api_key)])
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
            from flow_engine.upload import upload_video
            upload_res = await upload_video(temp_path, project_id, active_bridge)
            media_id = upload_res.get("mediaId") or upload_res.get("name") or upload_res.get("id")
            if not media_id and isinstance(upload_res.get("media"), dict):
                media_id = upload_res["media"].get("name") or upload_res["media"].get("mediaId")
            if not media_id:
                raise HTTPException(status_code=500, detail="Failed to upload video reference to Google Flow.")
        else:
            from flow_engine.generators.i2v import upload_image
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


@router.get("/v1/history")
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


@router.delete("/v1/history")
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


@router.delete("/v1/history/{filename}")
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


@router.get("/download/{filename}")
async def download_file(filename: str):
    """Serve the generated assets."""
    file_path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    media_type = "image/png"
    if filename.endswith(".mp4"):
        media_type = "video/mp4"
    return FileResponse(path=file_path, media_type=media_type)
