"""Omni Flash — Video to Video (V2V) editor."""

import logging
import random

from ..config import ENDPOINTS
from .common import build_client_context, build_generation_context

log = logging.getLogger("omniflash.generators.v2v")


async def edit_video(bridge, prompt: str, aspect: str, project_id: str,
                     video_media_id: str, fps: int = 24, duration: int = 10,
                     start_frame: int = 0, end_frame: int = None,
                     ref_media_ids: list[str] = None) -> list[str] | None:
    """Submit V2V edit request. Returns list of media_ids."""
    if end_frame is None:
        end_frame = fps * duration

    ref_images = [
        {"mediaId": mid, "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"}
        for mid in ref_media_ids
    ] if ref_media_ids else None

    req_item = {
        "aspectRatio": aspect,
        "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
        "videoModelKey": "abra_edit",
        "seed": random.randint(1, 9999),
        "metadata": {},
        "videoInput": {
            "mediaId": video_media_id,
            "startFrameIndex": start_frame,
            "endFrameIndex": end_frame,
        },
    }
    if ref_images:
        req_item["referenceImages"] = ref_images

    body = {
        "mediaGenerationContext": build_generation_context("BLOCK_SILENCED_VIDEOS"),
        "clientContext": build_client_context(project_id),
        "requests": [req_item],
    }

    log.info(' Editing: "%s" [abra_edit] media=%s', prompt[:50], video_media_id[:12])
    result = await bridge.api_request(ENDPOINTS["generate_edit"], body)

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
        log.error("Failed (%s): %s", status, err_msg)
        raise ValueError(err_msg)

    data = result.get("data", {})
    media = data.get("media", [])
    if not media:
        log.error("No media in response")
        return None

    media_ids = [m.get("name") for m in media]
    credits = data.get("remainingCredits", "?")
    log.info("Edit submitted! %d video(s), credits=%s", len(media_ids), credits)
    return media_ids
