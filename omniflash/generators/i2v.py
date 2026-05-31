"""Omni Flash — Image to Video (I2V) generator + image upload."""

import base64
import logging
import os
import random

from ..config import CLIENT_CTX, ENDPOINTS
from .. import media_store
from .common import build_client_context, build_generation_context

log = logging.getLogger("omniflash.generators.i2v")


async def upload_image(bridge, image_path: str, project_id: str = None) -> str | None:
    """Upload a local image to Flow. Returns media_id.

    Auto-saves filename → media_id to media-id.js.
    """
    from ..config import DEFAULT_PROJECT
    project_id = project_id or DEFAULT_PROJECT

    with open(image_path, "rb") as f:
        img_data = base64.b64encode(f.read()).decode()

    body = {
        "clientContext": {"tool": CLIENT_CTX["tool"], "projectId": project_id},
        "imageBytes": img_data,
    }

    log.info("📷 Uploading image: %s", os.path.basename(image_path))
    result = await bridge.api_request(ENDPOINTS["upload_image"], body)

    status = result.get("status", 0)
    data = result.get("data", {})
    if status != 200:
        err = data.get("error", {}).get("message", "Unknown") if isinstance(data, dict) else str(data)
        log.error("❌ Image upload failed (%s): %s", status, err)
        return None

    media_id = data.get("mediaId") or data.get("name")
    if not media_id and isinstance(data.get("media"), dict):
        media_id = data["media"].get("name")
    log.info("✅ Image uploaded! media_id=%s", media_id)

    if media_id:
        media_store.save(os.path.basename(image_path), media_id)

    return media_id


async def generate_video_i2v(bridge, prompt: str, aspect: str, project_id: str,
                              image_media_id: str, duration: int = 8) -> list[str] | None:
    """Generate video from a start image. Returns list of media_ids."""
    model_key = f"abra_t2v_{duration}s"

    body = {
        "mediaGenerationContext": build_generation_context(),
        "clientContext": build_client_context(project_id),
        "requests": [{
            "aspectRatio": aspect,
            "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
            "videoModelKey": model_key,
            "seed": random.randint(1, 9999),
            "metadata": {},
            "startImage": {"mediaId": image_media_id},
        }],
    }

    log.info('🖼️→🎬 I2V: "%s" [%s] image=%s', prompt[:50], model_key, image_media_id[:12])
    result = await bridge.api_request(ENDPOINTS["generate_i2v"], body)

    status = result.get("status", 0)
    if status != 200:
        err = result.get("data", {})
        if isinstance(err, dict):
            err = err.get("error", {}).get("message", result.get("error", "Unknown"))
        log.error("❌ I2V failed (%s): %s", status, err)
        return None

    data = result.get("data", {})
    media = data.get("media", [])
    if not media:
        log.error("❌ No media in response")
        return None

    media_ids = [m.get("name") for m in media]
    credits = data.get("remainingCredits", "?")
    log.info("✅ I2V submitted! %d video(s), credits=%s", len(media_ids), credits)
    return media_ids
