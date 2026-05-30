#!/usr/bin/env python3
"""Upload all chunks to Flow and get mediaIds."""

import asyncio
import base64
import json
import logging
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from omni import ExtensionBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("upload")

CHUNKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chunks")


async def upload_video(bridge, video_path):
    """Upload a video file via extension → /fx/api/upload-video."""
    with open(video_path, "rb") as f:
        video_b64 = base64.b64encode(f.read()).decode()

    size_mb = len(video_b64) * 3 / 4 / (1024 * 1024)
    log.info("📤 Uploading %s (%.1fMB)...", os.path.basename(video_path), size_mb)

    # Send to extension via WS
    req_id = str(uuid.uuid4())[:8]
    msg = {
        "id": req_id,
        "method": "upload_video",
        "params": {
            "videoBase64": video_b64,
            "projectId": "ff92d5cc-8a03-41d2-b59e-e0774d17bcf6",
        },
    }

    future = asyncio.get_event_loop().create_future()
    bridge._pending[req_id] = future

    await bridge._ws.send(json.dumps(msg))

    try:
        result = await asyncio.wait_for(future, timeout=120)
    except asyncio.TimeoutError:
        bridge._pending.pop(req_id, None)
        log.error("❌ Upload timeout")
        return None

    res = result.get("result", {})

    if res.get("error"):
        log.error("❌ Upload error: %s", res["error"])
        return None

    start_data = res.get("startData", {})
    upload_data = res.get("uploadData", {})

    log.info("📥 Start: %s", json.dumps(start_data, indent=2)[:300])
    log.info("📥 Upload: %s", json.dumps(upload_data, indent=2)[:300])

    # Extract mediaId from response
    media_id = upload_data.get("mediaId") or upload_data.get("name") or upload_data.get("id")
    if not media_id and isinstance(upload_data, dict):
        media = upload_data.get("media", {})
        if isinstance(media, dict):
            media_id = media.get("name") or media.get("mediaId")

    return {"media_id": media_id, "start_data": start_data, "upload_data": upload_data}


async def main():
    chunks_dir = sys.argv[1] if len(sys.argv) > 1 else CHUNKS_DIR

    # List chunks
    chunks = sorted([f for f in os.listdir(chunks_dir) if f.endswith(".mp4")])
    if not chunks:
        log.error("❌ No chunks found in %s", chunks_dir)
        return

    log.info("📁 Found %d chunks in %s", len(chunks), chunks_dir)

    bridge = ExtensionBridge()
    await bridge.start()

    if not await bridge.wait_for_extension(timeout=30):
        return

    results = {}

    for i, chunk in enumerate(chunks, 1):
        path = os.path.join(chunks_dir, chunk)
        log.info("─" * 50)
        log.info("[%d/%d] %s", i, len(chunks), chunk)

        result = await upload_video(bridge, path)
        if result:
            results[chunk] = result
            log.info("✅ %s → mediaId: %s", chunk, result.get("media_id", "?"))
        else:
            log.error("❌ %s failed", chunk)

    await bridge.close()

    # Save results
    out_file = os.path.join(os.path.dirname(chunks_dir), "media_ids.json")
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)

    log.info("─" * 50)
    log.info("🎉 Done! %d/%d uploaded", len(results), len(chunks))
    log.info("💾 Saved to: %s", out_file)

    for chunk, r in results.items():
        log.info("   %s → %s", chunk, r.get("media_id", "?"))


if __name__ == "__main__":
    asyncio.run(main())
