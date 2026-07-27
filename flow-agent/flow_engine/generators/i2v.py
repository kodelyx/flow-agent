"""Flow Engine — Image to Video (I2V) generator + image upload."""

import base64
import logging
import os
from ..config import CLIENT_CTX, ENDPOINTS
from .common import build_client_context, build_generation_context, resolve_seed
from flow_server.media_history import get_revalidated_upload_id, record_uploaded_media
from flow_server.media_types import sniff_media_type

log = logging.getLogger("flow_engine.generators.i2v")


async def upload_image(
    bridge,
    image_path: str,
    project_id: str = None,
    *,
    force_upload: bool = False,
) -> str | None:
    """Upload a local image to Flow. Returns media_id.

    A content-addressed cached ID is remotely revalidated before it is reused.
    """
    from ..config import DEFAULT_PROJECT
    project_id = project_id or DEFAULT_PROJECT
    image_path = os.path.abspath(image_path)
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    mime_type = sniff_media_type(image_path)
    if not mime_type.startswith("image/"):
        raise ValueError(
            f"Expected an image file, but {os.path.basename(image_path)} contains {mime_type}."
        )

    if not force_upload:
        cached_media_id = await get_revalidated_upload_id(
            image_path, bridge, project_id=project_id
        )
        if cached_media_id:
            return cached_media_id

    with open(image_path, "rb") as f:
        img_data = base64.b64encode(f.read()).decode()

    body = {
        "clientContext": {"tool": CLIENT_CTX["tool"], "projectId": project_id},
        "imageBytes": img_data,
    }

    log.info("Uploading image: %s", os.path.basename(image_path))
    result = await bridge.api_request(ENDPOINTS["upload_image"], body)

    status = result.get("status", 0)
    data = result.get("data", {})
    if status != 200:
        err = data.get("error", {}).get("message", "Unknown") if isinstance(data, dict) else str(data)
        err_msg = f"Image upload failed: {err}"
        log.error("%s", err_msg)
        raise ValueError(err_msg)

    media_id = data.get("mediaId") or data.get("name")
    if not media_id and isinstance(data.get("media"), dict):
        media_id = data["media"].get("name")
    log.info("Image uploaded! media_id=%s", media_id)

    if media_id:
        record_uploaded_media(
            image_path,
            media_id,
            project_id=project_id,
            mime_type=mime_type,
        )

    return media_id


async def generate_video_i2v(bridge, prompt: str, aspect: str, project_id: str,
                              image_media_id: str, duration: int = 8, count: int = 1,
                              seed: int = None, video_model: str = None) -> list[str] | None:
    """Generate video from a start image. Returns list of media_ids."""
    model_key = video_model or f"abra_t2v_{duration}s"

    requests = []
    for i in range(count):
        requests.append({
            "aspectRatio": aspect,
            "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
            "videoModelKey": model_key,
            "seed": resolve_seed(seed, i),
            "metadata": {},
            "startImage": {"mediaId": image_media_id},
        })

    body = {
        "mediaGenerationContext": build_generation_context(),
        "clientContext": build_client_context(project_id),
        "requests": requests,
    }

    log.info('I2V: "%s" [%s] image=%s count=%d', prompt[:50], model_key, image_media_id[:12], count)
    result = await bridge.api_request(ENDPOINTS["generate_i2v"], body)

    status = result.get("status", 0)
    if status != 200:
        err = result.get("data", {})
        reason = ""
        if isinstance(err, dict):
            details = err.get("error", {}).get("details", [])
            for detail in details:
                if isinstance(detail, dict) and "reason" in detail:
                    reason = f" ({detail['reason']})"
                    break
            err = err.get("error", {}).get("message", result.get("error", "Unknown"))
        err_msg = f"{err}{reason}"
        log.error("I2V failed (%s): %s", status, err_msg)
        raise ValueError(err_msg)

    data = result.get("data", {})
    media = data.get("media", [])
    if not media:
        log.error("No media in response")
        return None

    media_ids = [m.get("name") for m in media]
    credits = data.get("remainingCredits", "?")
    log.info("I2V submitted! %d video(s), credits=%s", len(media_ids), credits)
    return media_ids


async def generate_video_fl(bridge, prompt: str, aspect: str, project_id: str,
                             start_image_id: str, end_image_id: str,
                             duration: int = 8, count: int = 1,
                             seed: int = None,
                             video_model: str = None) -> list[str] | None:
    """Generate video with First+Last frame control.

    Video transitions smoothly from start_image to end_image.
    """
    model_key = video_model or f"abra_t2v_{duration}s"

    requests = []
    for i in range(count):
        requests.append({
            "aspectRatio": aspect,
            "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
            "videoModelKey": model_key,
            "seed": resolve_seed(seed, i),
            "metadata": {},
            "startImage": {"mediaId": start_image_id},
            "endImage": {"mediaId": end_image_id},
        })

    body = {
        "mediaGenerationContext": build_generation_context(),
        "clientContext": build_client_context(project_id),
        "requests": requests,
        "useV2ModelConfig": True,
    }

    log.info('FL: "%s" start=%s end=%s', prompt[:40], start_image_id[:12], end_image_id[:12])
    result = await bridge.api_request(ENDPOINTS["generate_fl"], body)

    status = result.get("status", 0)
    if status != 200:
        err = result.get("data", {})
        reason = ""
        if isinstance(err, dict):
            details = err.get("error", {}).get("details", [])
            for detail in details:
                if isinstance(detail, dict) and "reason" in detail:
                    reason = f" ({detail['reason']})"
                    break
            err = err.get("error", {}).get("message", result.get("error", "Unknown"))
        err_msg = f"{err}{reason}"
        log.error("FL failed (%s): %s", status, err_msg)
        raise ValueError(err_msg)

    data = result.get("data", {})
    media = data.get("media", [])
    if not media:
        log.error("No media in response")
        return None

    media_ids = [m.get("name") for m in media]
    credits = data.get("remainingCredits", "?")
    log.info("FL submitted! %d video(s), credits=%s", len(media_ids), credits)
    return media_ids


async def generate_video_r2v(bridge, prompt: str, aspect: str, project_id: str,
                              ref_media_ids: list[str],
                              duration: int = 8, count: int = 1,
                              seed: int = None, video_model: str = None) -> list[str] | None:
    """Generate video from reference images (character/style consistency)."""
    model_key = video_model or f"abra_t2v_{duration}s"

    ref_images = [
        {"mediaId": mid, "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"}
        for mid in ref_media_ids
    ]

    requests = []
    for i in range(count):
        requests.append({
            "aspectRatio": aspect,
            "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
            "videoModelKey": model_key,
            "seed": resolve_seed(seed, i),
            "metadata": {},
            "referenceImages": ref_images,
        })

    body = {
        "mediaGenerationContext": build_generation_context(),
        "clientContext": build_client_context(project_id),
        "requests": requests,
        "useV2ModelConfig": True,
    }

    log.info('R2V: "%s" refs=%d count=%d', prompt[:50], len(ref_media_ids), count)
    result = await bridge.api_request(ENDPOINTS["generate_r2v"], body)

    status = result.get("status", 0)
    if status != 200:
        err = result.get("data", {})
        reason = ""
        if isinstance(err, dict):
            details = err.get("error", {}).get("details", [])
            for detail in details:
                if isinstance(detail, dict) and "reason" in detail:
                    reason = f" ({detail['reason']})"
                    break
            err = err.get("error", {}).get("message", result.get("error", "Unknown"))
        err_msg = f"{err}{reason}"
        log.error("R2V failed (%s): %s", status, err_msg)
        raise ValueError(err_msg)

    data = result.get("data", {})
    media = data.get("media", [])
    if not media:
        log.error("No media in response")
        return None

    media_ids = [m.get("name") for m in media]
    credits = data.get("remainingCredits", "?")
    log.info("R2V submitted! %d video(s), credits=%s", len(media_ids), credits)
    return media_ids
