"""Omni Flash — Text to Video (T2V) generator."""

import logging
import random
import uuid

from ..config import ENDPOINTS
from .common import build_client_context, build_generation_context

log = logging.getLogger("omniflash.generators.t2v")


async def generate_video(bridge, prompt: str, aspect: str, project_id: str,
                         duration: int = 10, count: int = 1) -> list[str] | None:
    """Submit T2V generation request. Returns list of media_ids."""
    model_key = f"abra_t2v_{duration}s"

    requests = []
    for _ in range(count):
        requests.append({
            "aspectRatio": aspect,
            "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
            "videoModelKey": model_key,
            "seed": random.randint(1, 9999),
            "metadata": {},
        })

    body = {
        "mediaGenerationContext": build_generation_context(),
        "clientContext": build_client_context(project_id),
        "requests": requests,
        "useV2ModelConfig": True,
    }

    log.info('🎬 Generating: "%s" [%s] %ds x%d', prompt[:50], model_key, duration, count)
    result = await bridge.api_request(ENDPOINTS["generate_t2v"], body)

    status = result.get("status", 0)
    if status != 200:
        err = result.get("data", {})
        if isinstance(err, dict):
            err = err.get("error", {}).get("message", result.get("error", "Unknown"))
        log.error("❌ Failed (%s): %s", status, err)
        return None

    data = result.get("data", {})
    media = data.get("media", [])
    if not media:
        log.error("❌ No media in response")
        return None

    media_ids = [m.get("name") for m in media]
    credits = data.get("remainingCredits", "?")
    log.info("✅ Submitted! %d video(s), credits=%s", len(media_ids), credits)
    for mid in media_ids:
        log.info("   media_id=%s", mid)
    return media_ids
