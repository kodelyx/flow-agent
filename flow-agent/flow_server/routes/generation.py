#!/usr/bin/env python3
"""Image and video generation endpoints (OpenAI-compatible)."""

import os
import uuid
import time
import base64
import logging
import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Depends, Header
from fastapi.responses import JSONResponse

from flow_server.config import OUTPUT_DIR, map_size_to_aspect
from flow_server.models import (
    ImageGenerationRequest,
    VideoGenerationRequest,
    VideoGenerationResult,
    VideoUpsampleRequest,
)
from flow_server.idempotency import get_idempotency_store
from flow_server.jobs import get_job_store
from flow_server.history import MediaNotFoundError
from flow_server.media_history import resolve_media_reference
from flow_server.media_types import ensure_correct_extension, extension_for_media, sniff_media_type
from flow_server.state import verify_api_key, get_active_bridge, publish, append_to_history

from flow_engine import DEFAULT_PROJECT
from flow_engine.bridge import target_client_id_var
from flow_engine.config import CREDITS_PER_UPSAMPLE, CREDITS_PER_VIDEO, NATIVE_VIDEO_RESOLUTION
from flow_engine.generators.t2i import generate_image, download_image
from flow_engine.generators.upsample import normalise_resolution, upsample_video

# Setup logging (format configured centrally in flow_engine/__init__.py, imported above)
log = logging.getLogger("flow_engine.openai_api")

router = APIRouter()


def _request_payload(req, operation: str, x_client_id: Optional[str]) -> Dict[str, Any]:
    if hasattr(req, "model_dump"):
        request_data = req.model_dump(mode="json")
    else:
        request_data = req.dict()
    return {"operation": operation, "request": request_data, "client_id": x_client_id}


def _header_string(value: Any) -> Optional[str]:
    """FastAPI Header defaults remain Header objects in direct MCP calls."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _decode_base64_media(value: str) -> tuple[bytes, str]:
    """Decode a data URI/raw base64 value and classify it by its bytes."""
    payload = value
    declared_mime = ""
    if value.startswith("data:") and "," in value:
        metadata, payload = value.split(",", 1)
        declared_mime = metadata[5:].split(";", 1)[0]
    elif "," in value:
        payload = value.split(",", 1)[1]
    try:
        media_bytes = base64.b64decode("".join(payload.split()), validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 media: {exc}") from exc
    mime_type = sniff_media_type(media_bytes, declared_mime=declared_mime)
    return media_bytes, mime_type


@router.post("/v1/images/generations", dependencies=[Depends(verify_api_key)])
async def openai_generate_image(
    req: ImageGenerationRequest,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """Generate images once, replaying responses for identical retry keys."""
    x_client_id = _header_string(x_client_id)
    key = _header_string(idempotency_key)
    if not key:
        return await _generate_image(req, x_client_id)

    store = get_idempotency_store(OUTPUT_DIR)
    claim = await store.claim(key, _request_payload(req, "image", x_client_id))
    if claim.action == "conflict":
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used with a different image generation request.",
        )
    if claim.action == "replay":
        return claim.record["response"]
    if claim.action == "failed":
        error = claim.record.get("error", {})
        raise HTTPException(
            status_code=int(error.get("status_code", 500)),
            detail=error.get("detail", "The original idempotent image generation failed."),
        )
    if claim.action == "processing":
        raise HTTPException(
            status_code=409,
            detail="This idempotent image generation is already processing; retry with the same key.",
        )

    try:
        response = await _generate_image(req, x_client_id)
    except HTTPException as exc:
        await store.fail(key, exc.status_code, str(exc.detail))
        raise
    except Exception as exc:
        await store.fail(key, 500, str(exc))
        raise
    await store.succeed(key, response)
    return response


async def _generate_image(req: ImageGenerationRequest, x_client_id: Optional[str] = None):
    """Generate images from a prompt (OpenAI Spec)."""
    target_client_id_var.set(x_client_id)
    active_bridge = await get_active_bridge()
    aspect = map_size_to_aspect(req.size)
    project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

    ref_media_ids = req.ref_media_ids or None
    temp_img_path = None

    if ref_media_ids:
        try:
            ref_media_ids = [
                await resolve_media_reference(
                    ref,
                    active_bridge,
                    expected_type="image",
                    project_id=project_id,
                )
                for ref in ref_media_ids
            ]
        except MediaNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if req.image_base64 and not ref_media_ids:
        from flow_engine.generators.i2v import upload_image
        media_bytes, mime_type = _decode_base64_media(req.image_base64)
        if not mime_type.startswith("image/"):
            raise HTTPException(
                status_code=400,
                detail=f"I2I reference must be an image; received {mime_type}.",
            )

        timestamp = int(time.time())
        extension = extension_for_media(media_bytes, declared_mime=mime_type)
        temp_img_name = f"i2i_upload_{timestamp}_{uuid.uuid4().hex[:6]}{extension}"
        temp_img_path = os.path.join(OUTPUT_DIR, temp_img_name)

        try:
            with open(temp_img_path, "wb") as f:
                f.write(media_bytes)

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
        seed_offset = 0
        for chunk_size in chunks:
            tasks.append(
                generate_image(
                    active_bridge,
                    prompt=req.prompt,
                    aspect=aspect,
                    project_id=project_id,
                    count=chunk_size,
                    ref_media_ids=ref_media_ids,
                    model=req.model,
                    seed=(req.seed + seed_offset) % 4294967296 if req.seed is not None else None,
                )
            )
            seed_offset += chunk_size

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
            await append_to_history(
                "image",
                url,
                req.prompt,
                r.get("media_id"),
                None,
                project_id=project_id,
            )
            continue

        # Flow occasionally serves WebP/JPEG despite a PNG-looking URL.  This
        # is an auto-generated filename, so make its suffix match its bytes.
        out_path = ensure_correct_extension(out_path)
        filename = os.path.basename(out_path)

        if req.response_format == "b64_json":
            with open(out_path, "rb") as image_file:
                b64_data = base64.b64encode(image_file.read()).decode("utf-8")
                data_outputs.append({"b64_json": b64_data})
            # The response is inline, but the durable registry still needs the
            # generated media ID and local artifact for later references.
            served_url, r2_key = await publish(filename, out_path)
            await append_to_history(
                "image",
                served_url,
                req.prompt,
                r.get("media_id"),
                r2_key,
                local_path=out_path,
                project_id=project_id,
            )
        else:
            served_url, r2_key = await publish(filename, out_path)
            data_outputs.append({
                "url": served_url,
                "media_id": r.get("media_id"),
                "client_id": r.get("_client_id", ""),
            })
            await append_to_history(
                "image",
                served_url,
                req.prompt,
                r.get("media_id"),
                r2_key,
                local_path=out_path,
                project_id=project_id,
            )

    return {
        "created": timestamp,
        "data": data_outputs
    }


@router.post(
    "/v1/videos/generations",
    dependencies=[Depends(verify_api_key)],
    response_model=VideoGenerationResult,
    responses={202: {"model": VideoGenerationResult, "description": "Generation is already processing"}},
)
async def openai_generate_video(
    req: VideoGenerationRequest,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """Generate video and return a durable, pollable structured result."""
    x_client_id = _header_string(x_client_id)
    key = _header_string(idempotency_key)
    created = int(time.time())
    job_id = f"video_{uuid.uuid4().hex}"
    idem_store = get_idempotency_store(OUTPUT_DIR)
    job_store = get_job_store(OUTPUT_DIR)

    if key:
        claim = await idem_store.claim(
            key,
            _request_payload(req, "video", x_client_id),
            job_id=job_id,
            created=created,
        )
        if claim.action == "conflict":
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used with a different video generation request.",
            )
        if claim.action == "replay":
            return claim.record["response"]
        if claim.action == "failed":
            error = claim.record.get("error", {})
            raise HTTPException(
                status_code=int(error.get("status_code", 500)),
                detail=error.get("detail", "The original idempotent video generation failed."),
            )
        if claim.action == "processing":
            existing_job_id = claim.record.get("job_id")
            existing_job = await job_store.get(existing_job_id) if existing_job_id else None
            if existing_job and existing_job.get("status") == "succeeded":
                # Heal a crash between committing the job result and updating
                # its idempotency record.
                await idem_store.succeed(key, existing_job)
                return existing_job
            if existing_job and existing_job.get("status") == "failed":
                error = existing_job.get("error", {})
                await idem_store.fail(
                    key,
                    int(error.get("status_code", 500)),
                    error.get("detail", "The original video generation failed."),
                )
                raise HTTPException(
                    status_code=int(error.get("status_code", 500)),
                    detail=error.get("detail", "The original video generation failed."),
                )
            pending = {
                "job_id": existing_job_id,
                "status": "processing",
                "created": claim.record.get("created") or created,
                "data": [],
            }
            if existing_job_id and not existing_job:
                # The reservation is committed before the separate job file so
                # a crash can never duplicate a paid call. Recreate the polling
                # record when a retry observes that narrow crash window.
                await job_store.put(existing_job_id, pending)
            return JSONResponse(status_code=202, content=pending)

    pending = {"job_id": job_id, "status": "processing", "created": created, "data": []}
    await job_store.put(job_id, pending)

    try:
        generated = await _generate_video(req, x_client_id)
    except HTTPException as exc:
        error = {"status_code": exc.status_code, "detail": str(exc.detail)}
        await job_store.update(job_id, status="failed", error=error)
        if key:
            await idem_store.fail(key, exc.status_code, str(exc.detail))
        raise
    except Exception as exc:
        error = {"status_code": 500, "detail": str(exc)}
        await job_store.update(job_id, status="failed", error=error)
        if key:
            await idem_store.fail(key, 500, str(exc))
        raise

    result = dict(generated)
    result.update(job_id=job_id, status="succeeded")
    await job_store.put(job_id, result)
    if key:
        await idem_store.succeed(key, result)
    return result


@router.get(
    "/v1/videos/generations/{job_id}",
    dependencies=[Depends(verify_api_key)],
    response_model=VideoGenerationResult,
)
async def get_video_generation(job_id: str):
    """Poll a video generation result, including results persisted before restart."""
    job = await get_job_store(OUTPUT_DIR).get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Video generation job not found: {job_id}")
    return job


async def _resolve_upsample_tier(resolution) -> Optional[str]:
    """Validate a requested delivery resolution, returning the upsample tier."""
    try:
        return normalise_resolution(resolution)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _ensure_upsample_credits(active_bridge, tier: str, count: int) -> None:
    """Refuse a paid upsample the account cannot afford. 1080p is free."""
    cost_each = CREDITS_PER_UPSAMPLE.get(tier, 0)
    if cost_each <= 0:
        return
    try:
        cred_res = await active_bridge.api_request(
            "/v1/credits", body=None, captcha_action=None, method="GET"
        )
        cred_data = cred_res.get("data", cred_res) if isinstance(cred_res, dict) else {}
        balance = int(cred_data.get("credits", 0))
    except Exception:
        return
    if balance < cost_each * count:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Not enough credits: {balance} left, but {count} {tier} upsample(s) "
                f"cost {cost_each} each."
            ),
        )


async def _upsample_and_download(
    active_bridge,
    source_media_id: str,
    aspect_key: str,
    project_id: str,
    tier: str,
    *,
    prompt: str,
    seed: Optional[int] = None,
    index: int = 0,
):
    """Run one upsample pass end to end: submit, poll, download, record.

    Returns the new media entry, or raises ValueError with Flow's own message.
    """
    media_ids = await upsample_video(
        active_bridge,
        source_media_id,
        aspect_key,
        project_id,
        resolution=tier,
        seed=seed,
    )
    if not media_ids:
        raise ValueError(f"Flow accepted the {tier} upsample but returned no media.")

    from flow_engine.generators.common import poll_status, download_video

    upsampled_id = media_ids[0]
    if not await poll_status(active_bridge, upsampled_id, project_id):
        raise ValueError(f"The {tier} upsample of {source_media_id} did not finish.")

    timestamp = int(time.time())
    filename = f"flow_vid_{timestamp}_{uuid.uuid4().hex[:6]}_{index + 1}_{tier}.mp4"
    out_path = os.path.join(OUTPUT_DIR, filename)
    if not await download_video(active_bridge, upsampled_id, out_path):
        raise ValueError(f"Downloading the {tier} upsample of {upsampled_id} failed.")

    out_path = ensure_correct_extension(out_path)
    filename = os.path.basename(out_path)
    served_url, r2_key = await publish(filename, out_path)
    await append_to_history(
        "video",
        served_url,
        prompt,
        upsampled_id,
        r2_key,
        local_path=out_path,
        project_id=project_id,
    )
    return {
        "url": served_url,
        "media_id": upsampled_id,
        "resolution": tier,
        "source_media_id": source_media_id,
    }


@router.post(
    "/v1/videos/upsample",
    dependencies=[Depends(verify_api_key)],
    response_model=VideoGenerationResult,
    responses={202: {"model": VideoGenerationResult, "description": "Upsample is already processing"}},
)
async def openai_upsample_video(
    req: VideoUpsampleRequest,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """Upsample an existing Flow video to 1080p or 4K and download the result.

    Flow generates video at 720p; this is the second pass that the Flow UI runs
    behind its high-resolution download.
    """
    x_client_id = _header_string(x_client_id)
    key = _header_string(idempotency_key)
    created = int(time.time())
    job_id = f"upsample_{uuid.uuid4().hex}"
    idem_store = get_idempotency_store(OUTPUT_DIR)
    job_store = get_job_store(OUTPUT_DIR)

    if key:
        claim = await idem_store.claim(
            key,
            _request_payload(req, "upsample", x_client_id),
            job_id=job_id,
            created=created,
        )
        if claim.action == "conflict":
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used with a different upsample request.",
            )
        if claim.action == "replay":
            return claim.record["response"]
        if claim.action == "failed":
            error = claim.record.get("error", {})
            raise HTTPException(
                status_code=int(error.get("status_code", 500)),
                detail=error.get("detail", "The original idempotent upsample failed."),
            )
        if claim.action == "processing":
            existing_job_id = claim.record.get("job_id")
            existing_job = await job_store.get(existing_job_id) if existing_job_id else None
            if existing_job and existing_job.get("status") == "succeeded":
                await idem_store.succeed(key, existing_job)
                return existing_job
            if existing_job and existing_job.get("status") == "failed":
                error = existing_job.get("error", {})
                await idem_store.fail(
                    key,
                    int(error.get("status_code", 500)),
                    error.get("detail", "The original upsample failed."),
                )
                raise HTTPException(
                    status_code=int(error.get("status_code", 500)),
                    detail=error.get("detail", "The original upsample failed."),
                )
            pending = {
                "job_id": existing_job_id,
                "status": "processing",
                "created": claim.record.get("created") or created,
                "data": [],
            }
            if existing_job_id and not existing_job:
                await job_store.put(existing_job_id, pending)
            return JSONResponse(status_code=202, content=pending)

    pending = {"job_id": job_id, "status": "processing", "created": created, "data": []}
    await job_store.put(job_id, pending)

    try:
        generated = await _upsample_video(req, x_client_id)
    except HTTPException as exc:
        error = {"status_code": exc.status_code, "detail": str(exc.detail)}
        await job_store.update(job_id, status="failed", error=error)
        if key:
            await idem_store.fail(key, exc.status_code, str(exc.detail))
        raise
    except Exception as exc:
        error = {"status_code": 500, "detail": str(exc)}
        await job_store.update(job_id, status="failed", error=error)
        if key:
            await idem_store.fail(key, 500, str(exc))
        raise

    result = dict(generated)
    result.update(job_id=job_id, status="succeeded")
    await job_store.put(job_id, result)
    if key:
        await idem_store.succeed(key, result)
    return result


async def _upsample_video(req: VideoUpsampleRequest, x_client_id: Optional[str] = None):
    """Upsample one finished video to a higher delivery resolution."""
    target_client_id_var.set(x_client_id)
    active_bridge = await get_active_bridge()
    project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

    tier = await _resolve_upsample_tier(req.resolution)
    if tier is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Flow already delivers {NATIVE_VIDEO_RESOLUTION} natively; "
                "request '1080p' or '4k' to upsample."
            ),
        )

    from flow_engine import ASPECTS
    aspect_key = ASPECTS.get(req.aspect, "VIDEO_ASPECT_RATIO_PORTRAIT")

    try:
        source_media_id = await resolve_media_reference(
            req.media_id,
            active_bridge,
            expected_type="video",
            project_id=project_id,
        )
    except MediaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _ensure_upsample_credits(active_bridge, tier, 1)

    try:
        entry = await _upsample_and_download(
            active_bridge,
            source_media_id,
            aspect_key,
            project_id,
            tier,
            prompt=f"Upsampled to {tier}",
            seed=req.seed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"created": int(time.time()), "data": [entry]}


async def _generate_video(req: VideoGenerationRequest, x_client_id: Optional[str] = None):
    """Generate videos from a prompt (and optional start image)."""
    target_client_id_var.set(x_client_id)
    active_bridge = await get_active_bridge()
    project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

    # Reject an unusable resolution before anything is paid for.
    upsample_tier = await _resolve_upsample_tier(req.resolution)

    # Credit gate: only allow as many videos as the balance can afford.
    requested_n = req.n
    generation_count = requested_n
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
            generation_count = affordable

    # Map aspect ratio to Flow's ASPECT string
    from flow_engine import ASPECTS
    aspect_key = ASPECTS.get(req.aspect, "VIDEO_ASPECT_RATIO_PORTRAIT")

    from flow_engine.generators.common import poll_status, download_video
    image_media_id = req.start_media_id
    end_media_id = req.end_media_id
    ref_media_ids = list(req.ref_media_ids or [])
    temp_img_path = None
    is_video_input = bool(req.is_video)

    try:
        if image_media_id:
            image_media_id = await resolve_media_reference(
                image_media_id,
                active_bridge,
                expected_type="video" if is_video_input else "image",
                project_id=project_id,
            )
        if end_media_id:
            end_media_id = await resolve_media_reference(
                end_media_id,
                active_bridge,
                expected_type="image",
                project_id=project_id,
            )
        if ref_media_ids:
            ref_media_ids = [
                await resolve_media_reference(
                    ref,
                    active_bridge,
                    expected_type="image",
                    project_id=project_id,
                )
                for ref in ref_media_ids
            ]
    except MediaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # If image_base64 is provided and we don't have start_media_id, upload it first
    if req.image_base64 and not image_media_id:
        media_bytes, mime_type = _decode_base64_media(req.image_base64)
        is_video_input = mime_type.startswith("video/")
        if not (is_video_input or mime_type.startswith("image/")):
            raise HTTPException(
                status_code=400,
                detail=f"Start asset must be an image or video; received {mime_type}.",
            )

        timestamp = int(time.time())
        extension = extension_for_media(media_bytes, declared_mime=mime_type)
        temp_img_name = f"i2v_upload_{timestamp}_{uuid.uuid4().hex[:6]}{extension}"
        temp_img_path = os.path.join(OUTPUT_DIR, temp_img_name)

        try:
            with open(temp_img_path, "wb") as f:
                f.write(media_bytes)

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

    if end_media_id and not image_media_id:
        if temp_img_path and os.path.exists(temp_img_path):
            os.remove(temp_img_path)
        raise HTTPException(
            status_code=400,
            detail="end_media_id requires a start_media_id or image_base64 start image.",
        )
    if end_media_id and (is_video_input or ref_media_ids):
        if temp_img_path and os.path.exists(temp_img_path):
            os.remove(temp_img_path)
        raise HTTPException(
            status_code=400,
            detail="end_media_id supports first/last-frame image generation only.",
        )

    try:
        # Submit generation
        if is_video_input and image_media_id:
            from flow_engine.generators.v2v import edit_video
            media_ids = await edit_video(active_bridge, req.prompt, aspect_key, project_id, image_media_id, duration=req.duration, ref_media_ids=ref_media_ids or None)
        elif image_media_id and end_media_id:
            from flow_engine.generators.i2v import generate_video_fl
            media_ids = await generate_video_fl(
                active_bridge,
                req.prompt,
                aspect_key,
                project_id,
                start_image_id=image_media_id,
                end_image_id=end_media_id,
                duration=req.duration,
                count=generation_count,
                seed=req.seed,
                video_model=req.video_model,
            )
        elif ref_media_ids:
            from flow_engine.generators.i2v import generate_video_r2v
            media_ids = await generate_video_r2v(active_bridge, req.prompt, aspect_key, project_id, ref_media_ids, duration=req.duration, count=generation_count, seed=req.seed, video_model=req.video_model)
        elif image_media_id:
            from flow_engine.generators.i2v import generate_video_i2v
            media_ids = await generate_video_i2v(active_bridge, req.prompt, aspect_key, project_id, image_media_id, duration=req.duration, count=generation_count, seed=req.seed, video_model=req.video_model)
        else:
            from flow_engine.generators.t2v import generate_video
            media_ids = await generate_video(active_bridge, req.prompt, aspect_key, project_id, duration=req.duration, count=generation_count, seed=req.seed, video_model=req.video_model)
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

        out_path = ensure_correct_extension(out_path)
        filename = os.path.basename(out_path)

        served_url, r2_key = await publish(filename, out_path)
        return {
            "url": served_url,
            "media_id": media_id,
            "r2_key": r2_key,
            "local_path": out_path,
        }

    tasks = [poll_and_download(mid, i) for i, mid in enumerate(media_ids)]
    results = await asyncio.gather(*tasks)

    for r in results:
        if r:
            data_outputs.append({
                "url": r["url"],
                "media_id": r.get("media_id"),
                "resolution": NATIVE_VIDEO_RESOLUTION,
            })
            await append_to_history(
                "video",
                r["url"],
                req.prompt,
                r.get("media_id"),
                r.get("r2_key"),
                local_path=r.get("local_path"),
                project_id=project_id,
            )

    if not data_outputs:
        raise HTTPException(status_code=500, detail="Failed to complete video generations or downloads.")

    notes = []
    if requested_n != len(data_outputs):
        notes.append(
            f"Requested {requested_n} video(s); generated {len(data_outputs)} "
            f"(each {req.duration}s video costs {cost_each} credits)."
        )

    # Flow generates at 720p. A higher delivery resolution is a second
    # upsampler pass over the finished media, so it runs only once every clip
    # has landed. The 720p originals stay in history and in the response.
    if upsample_tier:
        await _ensure_upsample_credits(active_bridge, upsample_tier, len(data_outputs))

        async def upsample_entry(entry, index):
            source_id = entry.get("media_id")
            if not source_id:
                return None, "a generated clip had no media ID to upsample"
            try:
                return await _upsample_and_download(
                    active_bridge,
                    source_id,
                    aspect_key,
                    project_id,
                    upsample_tier,
                    prompt=req.prompt,
                    seed=req.seed,
                    index=index,
                ), None
            except (ValueError, RuntimeError) as exc:
                log.error("Upsample to %s failed for %s: %s", upsample_tier, source_id, exc)
                return None, str(exc)

        upsampled = await asyncio.gather(
            *(upsample_entry(entry, i) for i, entry in enumerate(data_outputs))
        )
        high_res = [entry for entry, _ in upsampled if entry]
        failures = [reason for entry, reason in upsampled if not entry and reason]

        if high_res:
            # Lead with the upsampled files so clients that consume data[0] as
            # "the" output write the high-resolution video.
            data_outputs = high_res + data_outputs
        if failures:
            notes.append(
                f"{len(failures)} clip(s) could not be upsampled to {upsample_tier} and are "
                f"returned at {NATIVE_VIDEO_RESOLUTION}: {failures[0]}"
            )

    resp = {
        "created": timestamp,
        "data": data_outputs
    }
    if notes:
        resp["note"] = " ".join(notes)
    return resp
