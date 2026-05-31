"""Flow Agent — Text to Image (T2I) generator.

Ported from virtual-try/go-server/image.go
"""

import logging
import os
import random
import re
import time

from ..config import ENDPOINTS, DEFAULT_PROJECT
from .common import build_client_context, build_generation_context

log = logging.getLogger("omniflash.generators.t2i")

# Image model (NARWHAL = Imagen 4 / Nano Banana 2)
IMAGE_MODEL = "NARWHAL"

# Image aspect ratios (more options than video)
IMAGE_ASPECTS = {
    "landscape": "IMAGE_ASPECT_RATIO_LANDSCAPE",   # 16:9
    "4x3":       "IMAGE_ASPECT_RATIO_4_3",          # 4:3
    "square":    "IMAGE_ASPECT_RATIO_SQUARE",        # 1:1
    "3x4":       "IMAGE_ASPECT_RATIO_3_4",           # 3:4
    "portrait":  "IMAGE_ASPECT_RATIO_PORTRAIT",      # 9:16
}

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _parse_image_results(data: dict) -> list[dict]:
    """Parse all images from batchGenerateImages response.
    
    Returns list of {"media_id": str, "image_url": str}
    """
    results = []
    media_list = data.get("media", [])
    
    for item in media_list:
        r = {"media_id": "", "image_url": ""}
        
        # media[i].name = mediaId
        name = item.get("name", "")
        if UUID_RE.match(name):
            r["media_id"] = name
        
        # media[i].image.generatedImage.fifeUrl
        img = item.get("image", {})
        gen = img.get("generatedImage", {})
        url = gen.get("fifeUrl", "") or gen.get("imageUri", "")
        if url:
            r["image_url"] = url
            # Fallback: extract mediaId from URL
            if not r["media_id"]:
                match = UUID_RE.search(url)
                if match:
                    r["media_id"] = match.group()
        
        results.append(r)
    
    return results


async def generate_image(bridge, prompt: str, aspect: str, project_id: str,
                         count: int = 1, ref_media_ids: list[str] = None) -> list[dict] | None:
    """Generate images from text prompt.
    
    Args:
        bridge: ExtensionBridge instance
        prompt: Text prompt for image
        aspect: Aspect ratio key (portrait/landscape/square/4x3/3x4)
        project_id: Flow project ID
        count: Number of variations (1-4)
        ref_media_ids: Optional reference image media IDs
    
    Returns:
        List of {"media_id": str, "image_url": str} or None on error
    """
    count = max(1, min(4, count))
    ts = int(time.time() * 1000)
    
    aspect_ratio = IMAGE_ASPECTS.get(aspect, aspect)
    
    # Build N items in requests array (1 API call = N images)
    requests = []
    for i in range(count):
        req_item = {
            "clientContext": build_client_context(project_id),
            "seed": (ts + i * 1000) % 1000000,
            "structuredPrompt": {"parts": [{"text": prompt}]},
            "imageAspectRatio": aspect_ratio,
            "imageModelName": IMAGE_MODEL,
        }
        
        # Add reference images if provided
        if ref_media_ids:
            req_item["imageInputs"] = [
                {"name": mid, "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"}
                for mid in ref_media_ids
            ]
        
        requests.append(req_item)
    
    body = {
        "clientContext": build_client_context(project_id),
        "requests": requests,
    }
    
    if ref_media_ids:
        body["mediaGenerationContext"] = build_generation_context()
        body["useNewMedia"] = True
    
    # Endpoint: /v1/projects/{projectId}/flowMedia:batchGenerateImages
    endpoint = f"/v1/projects/{project_id}/flowMedia:batchGenerateImages"
    
    log.info('🖼️  Generating: "%s" [%s] x%d', prompt[:50], aspect, count)
    result = await bridge.api_request(endpoint, body, captcha_action="IMAGE_GENERATION")
    
    status = result.get("status", 0)
    if status != 200:
        err = result.get("data", {})
        if isinstance(err, dict):
            err = err.get("error", {}).get("message", result.get("error", "Unknown"))
        log.error("❌ Failed (%s): %s", status, err)
        return None
    
    data = result.get("data", {})
    results = _parse_image_results(data)
    
    if not results:
        log.error("❌ No images in response")
        return None
    
    credits = data.get("remainingCredits", "?")
    log.info("✅ Generated! %d image(s), credits=%s", len(results), credits)
    for r in results:
        log.info("   media_id=%s", r["media_id"][:12] if r["media_id"] else "?")
    
    return results


async def download_image(bridge, image_url: str, output_path: str) -> bool:
    """Download image from fifeUrl via HTTP GET."""
    import urllib.request

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    try:
        urllib.request.urlretrieve(image_url, output_path)
        size_kb = os.path.getsize(output_path) / 1024
        log.info("✅ Saved: %s (%.0f KB)", output_path, size_kb)
        return True
    except Exception as e:
        log.error("❌ Download failed: %s", e)
        return False
