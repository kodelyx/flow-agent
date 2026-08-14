"""Flow Agent — Text to Image (T2I) generator.

Ported from virtual-try/go-server/image.go
"""

import logging
import os
import re
import time

from ..config import DEFAULT_IMAGE_MODEL, IMAGE_MODELS
from .common import build_client_context, build_generation_context
from flow_server.media_types import sniff_media_type

log = logging.getLogger("flow_engine.generators.t2i")

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
                         count: int = 1, ref_media_ids: list[str] = None,
                         model: str = None, seed: int = None) -> list[dict] | None:
    """Generate images from text prompt.

    Args:
        bridge: ExtensionBridge instance
        prompt: Text prompt for image
        aspect: Aspect ratio key (portrait/landscape/square/4x3/3x4)
        project_id: Flow project ID
        count: Number of variations (1-4)
        ref_media_ids: Optional reference image media IDs
        model: Optional image model name (harbor_seal, narwhal, gem_pix_2, etc.)
        seed: Optional explicit seed; reproducible when set, timestamp-derived when not

    Returns:
        List of {"media_id": str, "image_url": str} or None on error
    """
    count = max(1, min(4, count))
    ts = int(time.time() * 1000)

    aspect_ratio = IMAGE_ASPECTS.get(aspect, aspect)
    target_model = IMAGE_MODELS.get((model or "").lower(), DEFAULT_IMAGE_MODEL)

    # Build N items in requests array (1 API call = N images)
    requests = []
    for i in range(count):
        req_item = {
            "clientContext": build_client_context(project_id),
            "seed": ((int(seed) + i) % 4294967296) if seed is not None else (ts + i * 1000) % 1000000,
            "structuredPrompt": {"parts": [{"text": prompt}]},
            "imageAspectRatio": aspect_ratio,
            "imageModelName": target_model,
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

    log.info('  Generating: "%s" [%s] x%d', prompt[:50], aspect, count)
    result = await bridge.api_request(
        endpoint, body, captcha_action="IMAGE_GENERATION",
        meta={"prompt": prompt, "count": count},
    )

    client_id = result.get("_client_id", "") if isinstance(result, dict) else ""
    parsed = _parse_image_results(result.get("data", {}))
    for p in parsed:
        p["_client_id"] = client_id

    status = result.get("status", 0)
    if status != 200:
        if result.get("error") == "TIMEOUT":
            err_msg = (
                "timed out waiting for Google Flow to respond. The image may still "
                "have been generated — check labs.google/fx/tools/flow. Try again or "
                "raise API_REQUEST_TIMEOUT if this recurs."
            )
            log.error("Timeout: %s", err_msg)
            raise ValueError(err_msg)
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
    client_id = result.get("_client_id")
    results = _parse_image_results(data)

    if not results:
        log.error("No images in response")
        return None

    # Inject client_id into each result for history tracking
    for r in results:
        r["_model"] = target_model
        if client_id:
            r["_client_id"] = client_id

    credits = data.get("remainingCredits", "?")
    log.info("Generated! %d image(s), credits=%s", len(results), credits)
    for r in results:
        log.info("   media_id=%s", r["media_id"][:12] if r["media_id"] else "?")

    return results


async def download_image(bridge, image_url: str, output_path: str) -> bool:
    """Download a generated image to disk.

    The image URL is a public Google Storage signed URL, so it must be fetched
    directly — never through a corporate/HTTP proxy (those reject GCS with 403).
    A direct opener is used and the fetch is retried a few times to ride out
    transient network blips.
    """
    import urllib.request
    import time as _time

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Bypass any *_proxy env vars: signed GCS URLs 403 through most proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    last_err = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=60) as resp:
                image_bytes = resp.read()
                declared_mime = resp.headers.get("Content-Type", "")
            mime_type = sniff_media_type(
                image_bytes, declared_mime=declared_mime, filename=image_url
            )
            if not mime_type.startswith("image/"):
                raise ValueError(f"downloaded response is {mime_type}, not an image")
            with open(output_path, "wb") as f:
                f.write(image_bytes)
            size_kb = os.path.getsize(output_path) / 1024
            if size_kb <= 0:
                raise ValueError("empty download")
            log.info("Saved: %s (%.0f KB, %s)", output_path, size_kb, mime_type)
            return True
        except Exception as e:
            last_err = e
            log.warning("Download attempt %d/3 failed: %s", attempt, e)
            if attempt < 3:
                _time.sleep(1.5 * attempt)

    log.error("Download failed after 3 attempts: %s", last_err)
    return False
