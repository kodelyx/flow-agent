#!/usr/bin/env python3
"""Image and video generation endpoints (OpenAI-compatible)."""

import os
import uuid
import time
import base64
import logging
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Header

from flow_server.config import OUTPUT_DIR, map_size_to_aspect
from flow_server.models import ImageGenerationRequest, VideoGenerationRequest
from flow_server.state import verify_api_key, get_active_bridge, publish, append_to_history

from flow_engine import DEFAULT_PROJECT
from flow_engine.bridge import target_client_id_var
from flow_engine.config import CREDITS_PER_VIDEO
from flow_engine.generators.t2i import generate_image, download_image

# Setup logging (format configured centrally in flow_engine/__init__.py, imported above)
log = logging.getLogger("flow_engine.openai_api")

router = APIRouter()


@router.post("/v1/images/generations", dependencies=[Depends(verify_api_key)])
async def openai_generate_image(req: ImageGenerationRequest, x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    """Generate images from a prompt (OpenAI Spec)."""
    target_client_id_var.set(x_client_id)
    active_bridge = await get_active_bridge()
    aspect = map_size_to_aspect(req.size)
    project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

    ref_media_ids = req.ref_media_ids or None
    temp_img_path = None

    if req.image_base64 and not ref_media_ids:
        from flow_engine.generators.i2v import upload_image
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


@router.post("/v1/videos/generations", dependencies=[Depends(verify_api_key)])
async def openai_generate_video(req: VideoGenerationRequest, x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    """Generate videos from a prompt (and optional start image)."""
    target_client_id_var.set(x_client_id)
    active_bridge = await get_active_bridge()
    project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

    # Credit gate: only allow as many videos as the balance can afford.
    requested_n = req.n
    cost_each = CREDITS_PER_VIDEO.get(req.duration, 15)

    # Pin one client that can actually afford this video (free before pro,
    # richest-affordable first) so the credit check and the generation both run
    # on the SAME browser instead of a random one that might be broke.
    if not x_client_id:
        chosen = active_bridge._select_client_for_cost(cost_each)
        if chosen:
            target_client_id_var.set(chosen)

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
    from flow_engine import ASPECTS
    aspect_key = ASPECTS.get(req.aspect, "VIDEO_ASPECT_RATIO_PORTRAIT")

    from flow_engine.generators.common import poll_status, download_video
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
                from flow_engine.upload import upload_video
                upload_res = await upload_video(temp_img_path, project_id, active_bridge)
                image_media_id = upload_res.get("mediaId") or upload_res.get("name") or upload_res.get("id")
                if not image_media_id and isinstance(upload_res.get("media"), dict):
                    image_media_id = upload_res["media"].get("name") or upload_res["media"].get("mediaId")
                if not image_media_id:
                    raise HTTPException(status_code=500, detail="Failed to upload start video reference to Google Flow.")
            else:
                from flow_engine.generators.i2v import upload_image
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
            from flow_engine.generators.v2v import edit_video
            media_ids = await edit_video(active_bridge, req.prompt, aspect_key, project_id, image_media_id, duration=req.duration, ref_media_ids=req.ref_media_ids)
        elif req.ref_media_ids:
            from flow_engine.generators.i2v import generate_video_r2v
            media_ids = await generate_video_r2v(active_bridge, req.prompt, aspect_key, project_id, req.ref_media_ids, duration=req.duration, count=req.n)
        elif image_media_id:
            from flow_engine.generators.i2v import generate_video_i2v
            media_ids = await generate_video_i2v(active_bridge, req.prompt, aspect_key, project_id, image_media_id, duration=req.duration, count=req.n)
        else:
            from flow_engine.generators.t2v import generate_video
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
