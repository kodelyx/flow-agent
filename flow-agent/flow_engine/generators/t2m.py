"""Flow Engine — Text to Music (T2M) generator for Google Labs / MusicFX.

Handles prompt formatting, audio duration, looping, multi-endpoint fallback,
and media downloading/decoding.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
import urllib.request
import uuid
from typing import Any

from ..config import (
    CLIENT_CTX,
    DEFAULT_MUSIC_DURATION,
    DEFAULT_PROJECT,
    ENDPOINTS,
)
from .common import resolve_seed
from flow_server.media_types import ensure_correct_extension, sniff_media_type

log = logging.getLogger("flow_engine.generators.t2m")

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def build_music_client_context(project_id: str, tool: str = "MUSIC_FX") -> dict[str, Any]:
    """Build client context tailored for MusicFX / Google Labs audio requests."""
    return {
        "projectId": project_id or DEFAULT_PROJECT,
        "tool": tool,
        "userPaygateTier": CLIENT_CTX.get("tier", "PAYGATE_TIER_ONE"),
        "sessionId": f";{int(time.time() * 1000)}",
        "recaptchaContext": {
            "applicationType": CLIENT_CTX.get("recaptcha_app_type", "RECAPTCHA_APPLICATION_TYPE_WEB"),
            "token": "",
        },
    }


def _parse_music_results(data: dict[str, Any], prompt: str) -> list[dict[str, Any]]:
    """Parse generated audio tracks from varied Google Labs / MusicFX response formats.

    Returns a list of dicts with keys:
        media_id: str
        audio_url: str
        audio_base64: str
        revised_prompt: str
    """
    results: list[dict[str, Any]] = []

    # Format 1: "sounds" array (MusicFX / SoundDemo format)
    sounds = data.get("sounds", [])
    if isinstance(sounds, list) and sounds:
        for item in sounds:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            audio_b64 = (
                item.get("audio")
                or item.get("rawBytes")
                or item.get("audioContent")
                or item.get("bytes", "")
            )
            url = (
                item.get("downloadUrl")
                or item.get("fifeUrl")
                or item.get("audioUri")
                or item.get("url", "")
            )
            revised = item.get("revisedPrompt", prompt)
            media_id = name if UUID_RE.match(name) else ""
            if not media_id and url:
                match = UUID_RE.search(url)
                if match:
                    media_id = match.group()
            results.append({
                "media_id": media_id or name,
                "audio_url": url,
                "audio_base64": audio_b64,
                "revised_prompt": revised,
            })

    # Format 2.5: "predictions" array
    predictions = data.get("predictions", [])
    if isinstance(predictions, list) and predictions:
        for item in predictions:
            if not isinstance(item, dict):
                continue
            audio_b64 = (
                item.get("bytesBase64Encoded")
                or item.get("audio")
                or item.get("rawBytes", "")
            )
            url = item.get("downloadUrl") or item.get("url", "")
            name = item.get("name", "")
            results.append({
                "media_id": name or str(uuid.uuid4()),
                "audio_url": url,
                "audio_base64": audio_b64,
                "revised_prompt": prompt,
            })

    # Format 2: "media" array (Pinhole / Flow style)
    media_list = data.get("media", [])
    if isinstance(media_list, list) and media_list:
        for item in media_list:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            audio_obj = item.get("audio", {})
            gen = audio_obj.get("generatedAudio", {}) if isinstance(audio_obj, dict) else {}
            url = (
                gen.get("downloadUrl")
                or gen.get("fifeUrl")
                or gen.get("audioUri")
                or item.get("downloadUrl", "")
            )
            audio_b64 = (
                gen.get("rawBytes")
                or gen.get("audioContent")
                or item.get("rawBytes", "")
            )
            media_id = name if UUID_RE.match(name) else ""
            if not media_id and url:
                match = UUID_RE.search(url)
                if match:
                    media_id = match.group()
            results.append({
                "media_id": media_id or name,
                "audio_url": url,
                "audio_base64": audio_b64,
                "revised_prompt": prompt,
            })

    # Format 3: Direct single audio response
    if not results:
        direct_b64 = (
            data.get("audioContent")
            or data.get("rawBytes")
            or data.get("audio", "")
        )
        direct_url = data.get("downloadUrl") or data.get("audioUri", "")
        if direct_b64 or direct_url:
            results.append({
                "media_id": data.get("name", ""),
                "audio_url": direct_url,
                "audio_base64": direct_b64 if isinstance(direct_b64, str) else "",
                "revised_prompt": prompt,
            })

    return results


async def _generate_flowmusic(
    bridge,
    prompt: str,
    model: str | None = None,
) -> list[dict[str, Any]] | None:
    """Generate music using Flow Music (flowmusic.app)."""
    conv_url = ENDPOINTS.get("flowmusic_conversation", "https://www.flowmusic.app/__api/conversation")
    payload = {
        "parts": [{"content": prompt, "part_kind": "user-prompt"}],
        "client_context": {
            "song_queue": [],
            "selected_model": None,
            "lyrics_id_map": {},
            "ghostwriter_version": "standard",
        },
        "model_name": model or "producer:standard",
        "mode": "standard",
    }

    log.info('Submitting prompt to Flow Music (flowmusic.app): "%s"', prompt[:60])
    resp = await bridge.api_request(conv_url, payload, method="POST")
    status = resp.get("status", 0)
    data = resp.get("data", {})

    if status not in (200, 201) or not isinstance(data, dict):
        err = data if data else resp.get("error", f"HTTP {status}")
        log.error("Flow Music conversation endpoint failed: status=%s data=%s", status, err)
        raise ValueError(f"Flow Music conversation creation failed (HTTP {status}): {err}")

    # Check if direct results were returned in data
    direct_parsed = _parse_music_results(data, prompt)
    if direct_parsed:
        return direct_parsed

    job_id = data.get("job_id")
    if not job_id:
        log.error("Flow Music response contained no job_id: %s", data)
        raise ValueError(f"Flow Music response missing job_id: {data}")

    log.info("Flow Music job created: %s, streaming events...", job_id)
    stream_url = ENDPOINTS.get(
        "flowmusic_stream",
        "https://www.flowmusic.app/__api/messages/{job_id}/stream?last_id=0"
    ).format(job_id=job_id)

    # Await event stream resolution via extension
    stream_resp = await bridge.api_request(stream_url, {}, method="GET", timeout=120)
    stream_data = stream_resp.get("data", {})
    clip_ids = []
    if isinstance(stream_data, dict):
        clip_ids = stream_data.get("clip_ids", [])

    if not clip_ids:
        log.error("Flow Music stream returned no clip_ids: %s", stream_resp)
        raise ValueError(f"Flow Music stream completed without clip_ids: {stream_resp}")

    log.info("Flow Music produced %d clip(s): %s. Fetching details...", len(clip_ids), clip_ids)
    clips_url = ENDPOINTS.get("flowmusic_clips", "https://www.flowmusic.app/__api/clips")
    clips_resp = await bridge.api_request(clips_url, {"clip_ids": clip_ids}, method="POST")

    if clips_resp.get("status") != 200:
        err = clips_resp.get("data") or clips_resp.get("error", f"HTTP {clips_resp.get('status')}")
        log.error("Flow Music clips endpoint failed (HTTP %s): %s", clips_resp.get("status"), err)
        raise ValueError(f"Flow Music clips endpoint failed (HTTP {clips_resp.get('status')}): {err}")

    clips_data = clips_resp.get("data", {})
    clips_dict = clips_data.get("clips", {}) if isinstance(clips_data, dict) else {}

    results = []
    for cid, clip in clips_dict.items():
        if not isinstance(clip, dict):
            continue
        audio_url = clip.get("audio_url") or clip.get("wav_url", "")
        wav_url = clip.get("wav_url") or clip.get("audio_url", "")
        title = clip.get("title", "")
        duration_obj = clip.get("duration", {})
        duration_sec = 0.0
        if isinstance(duration_obj, dict) and duration_obj.get("value"):
            try:
                duration_sec = float(duration_obj["value"])
            except (ValueError, TypeError):
                duration_sec = 0.0

        results.append({
            "media_id": cid,
            "audio_url": audio_url,
            "wav_url": wav_url,
            "title": title,
            "duration": duration_sec,
            "audio_base64": "",
            "revised_prompt": prompt,
        })

    if results:
        log.info("Flow Music generated %d tracks successfully!", len(results))
    return results if results else None


async def generate_music(
    bridge,
    prompt: str,
    project_id: str | None = None,
    duration: int = DEFAULT_MUSIC_DURATION,
    count: int = 1,
    loop: bool = False,
    seed: int | None = None,
    model: str | None = None,
) -> list[dict[str, Any]] | None:
    """Submit a music generation request to Flow Music (flowmusic.app) or Google Labs.

    Args:
        bridge: ExtensionBridge instance
        prompt: Text prompt describing the music, mood, genre, or instruments
        project_id: Flow project ID
        duration: Desired duration in seconds (e.g. 10, 30, 50, 70)
        count: Number of variations (1-4)
        loop: Whether to generate a seamless loop
        seed: Optional explicit random seed
        model: Optional model override (e.g. producer:standard, MUSIC_FX)

    Returns:
        List of dicts with keys (media_id, audio_url, audio_base64, revised_prompt) or None on failure
    """
    # Primary engine: Flow Music (flowmusic.app)
    if model not in ("MUSIC_FX", "MUSICLM_V2", "DEFAULT"):
        try:
            flowmusic_results = await _generate_flowmusic(bridge, prompt, model=model)
            if flowmusic_results:
                return flowmusic_results
        except Exception as exc:
            log.warning("Flow Music generation failed, falling back to Google Labs MusicFX: %s", exc)

    # 2. Fallback: Google Labs AISandbox endpoints
    proj_id = project_id or DEFAULT_PROJECT
    count = max(1, min(4, count))
    seed_val = resolve_seed(seed)

    primary_body = {
        "clientContext": build_music_client_context(proj_id, tool=model or "MUSICLM_V2"),
        "generationCount": count,
        "input": {
            "textInput": prompt,
        },
        "soundLengthSeconds": int(duration),
        "loop": bool(loop),
        "model": "DEFAULT",
        "seed": seed_val,
    }
    alt_body = {
        "clientContext": build_music_client_context(proj_id, tool=model or "MUSIC_FX"),
        "generationCount": count,
        "inputContext": {
            "textInput": prompt,
        },
        "soundLengthSeconds": int(duration),
        "loop": bool(loop),
        "seed": seed_val,
    }

    log.info('Generating music via fallback: "%s" (%ds, loop=%s, count=%d)', prompt[:50], duration, loop, count)

    candidate_configs = [
        (ENDPOINTS.get("generate_music", "/v1:runMusicFx"), primary_body),
        (ENDPOINTS.get("generate_music", "/v1:runMusicFx"), alt_body),
        (ENDPOINTS.get("generate_music_demo", "/v1:runSoundDemo"), primary_body),
        (ENDPOINTS.get("generate_music_sound", "/v1/sound:generate"), alt_body),
        (ENDPOINTS.get("generate_music_batch", "/v1/music:batchGenerateMusic"), alt_body),
    ]

    attempt_errors = []
    for endpoint, req_body in candidate_configs:
        try:
            result = await bridge.api_request(endpoint, req_body, captcha_action="MUSIC_GENERATION")
            status = result.get("status", 0)
            data = result.get("data", {})
            err_msg = ""
            if isinstance(data, dict):
                err_msg = data.get("error", {}).get("message") or data.get("message") or str(data)
            elif isinstance(data, str):
                err_msg = data
            err_msg = err_msg or result.get("error", f"HTTP {status}")

            log.info("Music endpoint %s response: status=%s, error=%s", endpoint, status, err_msg[:200])

            if status == 200:
                parsed = _parse_music_results(data if isinstance(data, dict) else {}, prompt)
                if parsed:
                    log.info("Music generated successfully! (%d tracks)", len(parsed))
                    return parsed
                else:
                    log.warning("Music endpoint returned 200 but parse found no audio tracks: %s", str(data)[:200])
                    attempt_errors.append(f"{endpoint}: 200 OK but no audio in payload")
            else:
                attempt_errors.append(f"{endpoint} -> HTTP {status}: {err_msg}")
        except Exception as exc:
            log.warning("Request to %s encountered exception: %s", endpoint, exc)
            attempt_errors.append(f"{endpoint} -> Exception: {exc}")

    raise ValueError(f"Google Labs Music API error: {'; '.join(attempt_errors)}")


async def download_music(
    bridge,
    media_id_or_url: str,
    output_path: str,
    audio_base64: str | None = None,
) -> str | None:
    """Save generated audio to disk from base64 content, signed URL, or media ID.

    Returns the absolute path of the saved audio file, or None on failure.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    destination = os.path.abspath(output_path)

    # Path 1: Direct base64 data
    if audio_base64:
        try:
            raw_payload = audio_base64
            declared_mime = "audio/mpeg"
            if raw_payload.startswith("data:") and "," in raw_payload:
                meta, raw_payload = raw_payload.split(",", 1)
                declared_mime = meta[5:].split(";", 1)[0]
            audio_bytes = base64.b64decode("".join(raw_payload.split()), validate=False)
            with open(destination, "wb") as f:
                f.write(audio_bytes)
            final_path = ensure_correct_extension(destination, declared_mime=declared_mime)
            return final_path
        except Exception as exc:
            log.error("Failed to decode base64 audio: %s", exc)

    # Path 2: Download from URL
    target_url = media_id_or_url
    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        # Path 3: Media ID resolution
        signed_url = await bridge.request_media_url(media_id_or_url)
        if signed_url:
            target_url = signed_url
        else:
            log.error("Could not obtain download URL for media ID: %s", media_id_or_url)
            return None

    try:
        req = urllib.request.Request(
            target_url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        with urllib.request.urlopen(req, timeout=90) as response:
            audio_bytes = response.read()

        mime = sniff_media_type(audio_bytes, filename=destination)
        with open(destination, "wb") as f:
            f.write(audio_bytes)
        final_path = ensure_correct_extension(destination, declared_mime=mime)
        return final_path
    except Exception as exc:
        log.error("Failed to download audio from %s: %s", target_url, exc)
        return None
