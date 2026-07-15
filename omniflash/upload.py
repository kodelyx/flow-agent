"""Omni Flash — Video upload via GCS resumable upload.

Uploads a video file to Google Flow using the tRPC session URL
and curl for the binary PUT.
"""

import asyncio
import json
import logging
import os
import subprocess
import uuid

from .bridge import ExtensionBridge
from .config import DEFAULT_PROJECT
from . import media_store

log = logging.getLogger("omniflash.upload")

DEFAULT_PROJECT_ID = DEFAULT_PROJECT


async def upload_video(video_path: str, project_id: str = DEFAULT_PROJECT_ID,
                       bridge: ExtensionBridge = None) -> dict:
    """Upload a video file to Google Flow.

    If bridge is provided, uses it (caller manages lifecycle).
    Otherwise creates and closes its own bridge.

    Returns dict with mediaId, media, workflow on success.
    """
    video_path = os.path.abspath(video_path)
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    video_size = os.path.getsize(video_path)
    log.info("Video: %s (%d bytes, %.1fMB)",
             os.path.basename(video_path), video_size, video_size / 1024 / 1024)

    # Manage bridge lifecycle
    own_bridge = bridge is None
    if own_bridge:
        bridge = ExtensionBridge()
        await bridge.start()
        if not await bridge.wait_for_extension(timeout=30):
            raise ConnectionError("Extension did not connect")
        await asyncio.sleep(2)

    # Step 1: Get session URL via extension's trpc_request
    token = bridge._flow_key
    rid = str(uuid.uuid4())[:8]
    fut = asyncio.get_event_loop().create_future()
    bridge._pending[rid] = fut

    await bridge._ws.send(json.dumps({
        "id": rid,
        "method": "trpc_request",
        "params": {
            "url": "https://labs.google/fx/api/upload-video?action=start",
            "method": "POST",
            "headers": {
                "X-Upload-Project-Id": project_id,
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(video_size),
            },
        },
    }))

    try:
        r = await asyncio.wait_for(fut, timeout=20)
    except asyncio.TimeoutError:
        if own_bridge:
            await bridge.close()
        raise TimeoutError("Step 1 timeout")

    if own_bridge:
        await bridge.close()

    session_url = r.get("data", {}).get("sessionUrl", "")
    auth_token = token or ""

    if not session_url:
        raise RuntimeError(f"No session URL: {json.dumps(r)[:500]}")

    log.info("Got session URL")

    # Step 2: PUT via curl (async, non-blocking)
    log.info("Uploading %d bytes via curl...", video_size)
    proc = await asyncio.create_subprocess_exec(
        "curl", "-X", "PUT", session_url,
        "-H", "Content-Type: video/mp4",
        "-H", f"Authorization: Bearer {auth_token}",
        "-H", "X-Goog-Upload-Command: upload, finalize",
        "-H", "X-Goog-Upload-Offset: 0",
        "--data-binary", f"@{video_path}",
        "--max-time", "120",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"curl failed: {stderr.decode('utf-8', errors='ignore')[:500]}")

    stdout_str = stdout.decode('utf-8', errors='ignore')
    if not stdout_str.strip():
        raise RuntimeError("Empty response from upload")

    data = json.loads(stdout_str)
    media_id = data.get("mediaId") or data.get("name") or data.get("id")
    if not media_id and isinstance(data.get("media"), dict):
        media_id = data["media"].get("name") or data["media"].get("mediaId")

    if media_id:
        log.info("SUCCESS! media_id = %s", media_id)
        media_store.save(os.path.basename(video_path), media_id)
    else:
        log.warning("Upload succeeded but no media_id found")
        log.info("Response: %s", json.dumps(data, indent=2)[:1000])

    return data
