"""Flow Engine — Video upsampler (720p -> 1080p / 4K).

Flow generates video at 720p. The higher-resolution download offered in the
Flow UI is a second pass over the finished media against an upsampler model,
submitted asynchronously and polled with the ordinary video status endpoint.

The request shape below is part of Google's undocumented Flow API. The
resolution enum in particular is not published, so a rejected enum is retried
with the known spelling variants and finally omitted entirely — the upsampler
model key already encodes the target resolution. A rejected request never
starts a generation, so the ladder cannot double-charge.
"""

import logging

from ..config import (
    ENDPOINTS,
    VIDEO_UPSAMPLE_MODELS,
    VIDEO_UPSAMPLE_RESOLUTIONS,
)
from .common import build_client_context, build_generation_context, resolve_seed

log = logging.getLogger("flow_engine.generators.upsample")

# Ordered spelling candidates per tier. The configured value is always tried
# first; ``None`` means "send no resolution field at all".
_RESOLUTION_CANDIDATES = {
    "1080p": ("VIDEO_RESOLUTION_1080P", "VIDEO_RESOLUTION_1080p", None),
    "4k": ("VIDEO_RESOLUTION_4K", "VIDEO_RESOLUTION_4k", None),
}

# Substrings that identify a request rejected for its shape rather than for a
# real generation failure (bad media ID, no credits, unusual activity...).
_SCHEMA_REJECTION_HINTS = (
    "resolution",
    "invalid value",
    "invalid_argument",
    "unknown name",
    "cannot find field",
)


def normalise_resolution(resolution) -> str | None:
    """Map a user-facing resolution to an upsample tier key, or None.

    Returns None for the native 720p output, which needs no upsample pass.
    """
    key = str(resolution or "").strip().lower().replace(" ", "")
    if not key or key in {"720p", "720", "native", "source", "original"}:
        return None
    aliases = {
        "1080": "1080p",
        "1080p": "1080p",
        "fhd": "1080p",
        "hd": "1080p",
        "full_hd": "1080p",
        "fullhd": "1080p",
        "4k": "4k",
        "2160": "4k",
        "2160p": "4k",
        "uhd": "4k",
    }
    tier = aliases.get(key)
    if tier is None:
        raise ValueError(
            f"Unsupported video resolution {resolution!r}; use '720p', '1080p', or '4k'."
        )
    return tier


def _resolution_candidates(tier: str) -> list[str | None]:
    configured = VIDEO_UPSAMPLE_RESOLUTIONS.get(tier)
    ordered: list[str | None] = [configured] if configured else []
    for candidate in _RESOLUTION_CANDIDATES.get(tier, ()):
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _error_message(result: dict) -> str:
    data = result.get("data", {})
    reason = ""
    message = result.get("error", "Unknown")
    if isinstance(data, dict):
        error = data.get("error", {})
        if isinstance(error, dict):
            message = error.get("message", message)
            for detail in error.get("details", []) or []:
                if isinstance(detail, dict) and "reason" in detail:
                    reason = f" ({detail['reason']})"
                    break
        elif data:
            message = data
    return f"{message}{reason}"


def _looks_like_schema_rejection(status: int, message: str) -> bool:
    if status not in (400, 404):
        return False
    lowered = message.lower()
    return any(hint in lowered for hint in _SCHEMA_REJECTION_HINTS)


def _parse_media_ids(data: dict) -> list[str]:
    """Collect upsampled media IDs from either response shape."""
    if not isinstance(data, dict):
        return []

    ids: list[str] = []

    def _add(value):
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)

    for item in data.get("media", []) or []:
        if isinstance(item, dict):
            _add(item.get("name") or item.get("mediaId") or item.get("id"))

    # Older/legacy responses nest the new media under operations.
    for item in data.get("operations", []) or []:
        if not isinstance(item, dict):
            continue
        _add(item.get("name") or item.get("mediaId"))
        nested = item.get("media") or item.get("operation")
        if isinstance(nested, dict):
            _add(nested.get("name") or nested.get("mediaId"))

    return ids


async def upsample_video(
    bridge,
    media_id: str,
    aspect: str,
    project_id: str,
    resolution: str = "1080p",
    seed: int = None,
    scene_id: str = None,
) -> list[str] | None:
    """Submit an upsample pass for one finished video. Returns new media IDs.

    ``resolution`` accepts '1080p' or '4k'. Poll the returned IDs with
    ``flow_engine.generators.common.poll_status`` and download them the same way
    as any generated video.
    """
    tier = normalise_resolution(resolution)
    if tier is None:
        raise ValueError(
            "Upsampling to 720p is a no-op; Flow already generates at 720p."
        )

    model_key = VIDEO_UPSAMPLE_MODELS.get(tier)
    if not model_key:
        raise ValueError(f"No upsampler model configured for {tier}.")

    last_error = None
    for attempt, resolution_enum in enumerate(_resolution_candidates(tier)):
        request_item = {
            "aspectRatio": aspect,
            "videoModelKey": model_key,
            "seed": resolve_seed(seed),
            "metadata": {"sceneId": scene_id} if scene_id else {},
            "videoInput": {"mediaId": media_id},
        }
        if resolution_enum is not None:
            request_item["resolution"] = resolution_enum

        body = {
            "mediaGenerationContext": build_generation_context(),
            "clientContext": build_client_context(project_id),
            "requests": [request_item],
        }

        log.info(
            "Upsampling %s to %s [%s]%s",
            media_id[:12],
            tier,
            model_key,
            "" if resolution_enum is None else f" resolution={resolution_enum}",
        )
        result = await bridge.api_request(
            ENDPOINTS["upsample_video"], body, captcha_action="VIDEO_GENERATION"
        )

        status = result.get("status", 0)
        if status == 200:
            media_ids = _parse_media_ids(result.get("data", {}))
            if not media_ids:
                log.error("Upsample accepted but returned no media: %r", result.get("data"))
                return None
            log.info("Upsample submitted! %s -> %s", media_id[:12], ", ".join(media_ids))
            return media_ids

        last_error = _error_message(result)
        if _looks_like_schema_rejection(status, last_error) and attempt + 1 < len(
            _resolution_candidates(tier)
        ):
            log.warning(
                "Upsample rejected the request shape (%s): %s — retrying with the "
                "next resolution spelling.",
                status,
                last_error,
            )
            continue

        log.error("Upsample failed (%s): %s", status, last_error)
        raise ValueError(last_error)

    raise ValueError(last_error or "Upsample request was rejected.")
