#!/usr/bin/env python3
"""
Omni Flash — Full Video Editor
Edits an entire video by splitting into 10s segments,
processing each through V2V (abra_edit), and saving results.

Usage:
    python edit_full.py "Make it anime style" --media-id <ID> --video-file input.mp4 -o output/
    python edit_full.py "Cyberpunk neon look" --media-id <ID> --total-seconds 45 -o output/
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time

# Import everything from omni.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from omni import (
    ExtensionBridge, edit_video, poll_status, download_video,
    ASPECTS, DEFAULT_PROJECT
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("edit_full")

SEGMENT_DURATION = 10  # seconds per segment
FPS = 24               # frames per second


def get_video_duration(video_path):
    """Get video duration in seconds using ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True
        )
        return float(r.stdout.strip())
    except Exception:
        log.warning("ffprobe not found or failed, trying python...")
        # Fallback: rough estimate from file size
        try:
            size = os.path.getsize(video_path)
            # Rough: 1MB per 3 seconds for typical video
            return max(10, size / (1024 * 1024) * 3)
        except Exception:
            return None


def get_video_fps(video_path):
    """Get video FPS using ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True
        )
        fps_str = r.stdout.strip()
        if "/" in fps_str:
            num, den = fps_str.split("/")
            return float(num) / float(den)
        return float(fps_str)
    except Exception:
        return FPS


async def edit_segment(bridge, prompt, aspect, project_id, media_id,
                       start_frame, end_frame, segment_num, output_dir):
    """Edit a single segment and download."""
    import uuid
    import random
    from omni import ENDPOINTS, CLIENT_CTX, API_KEY, API_BASE

    body = {
        "mediaGenerationContext": {
            "batchId": str(uuid.uuid4()),
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
        },
        "clientContext": {
            "projectId": project_id,
            "tool": CLIENT_CTX["tool"],
            "userPaygateTier": CLIENT_CTX["tier"],
            "sessionId": f";{int(time.time() * 1000)}",
            "recaptchaContext": {
                "applicationType": CLIENT_CTX["recaptcha_app_type"],
                "token": "",
            },
        },
        "requests": [{
            "aspectRatio": aspect,
            "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
            "videoModelKey": "abra_edit",
            "seed": random.randint(1, 9999),
            "metadata": {},
            "videoInput": {
                "mediaId": media_id,
                "startFrameIndex": start_frame,
                "endFrameIndex": end_frame,
            },
        }],
    }

    start_sec = start_frame / FPS
    end_sec = end_frame / FPS
    log.info("✂️ Segment %d: %.0fs-%.0fs (frames %d-%d)",
             segment_num, start_sec, end_sec, start_frame, end_frame)

    result = await bridge.api_request(ENDPOINTS["generate_edit"], body)

    status = result.get("status", 0)
    if status != 200:
        err = result.get("data", {})
        if isinstance(err, dict):
            err = err.get("error", {}).get("message", result.get("error", "Unknown"))
        log.error("❌ Segment %d failed (%s): %s", segment_num, status, err)
        return None

    data = result.get("data", {})
    media_list = data.get("media", [])
    if not media_list:
        log.error("❌ No media for segment %d", segment_num)
        return None

    result_media_id = media_list[0].get("name")
    credits = data.get("remainingCredits", "?")
    log.info("✅ Segment %d submitted! media_id=%s, credits=%s",
             segment_num, result_media_id[:12], credits)

    # Poll
    if not await poll_status(bridge, result_media_id, project_id):
        return None

    # Download
    out_path = os.path.join(output_dir, f"segment_{segment_num:03d}.mp4")
    if await download_video(bridge, result_media_id, out_path):
        return out_path
    return None


async def run(args):
    aspect = ASPECTS.get(args.aspect, "VIDEO_ASPECT_RATIO_PORTRAIT")

    # Get total duration
    total_seconds = args.total_seconds
    fps = FPS

    if args.video_file and os.path.exists(args.video_file):
        if not total_seconds:
            total_seconds = get_video_duration(args.video_file)
            log.info("📹 Video: %s (%.1fs)", args.video_file, total_seconds or 0)
        fps = get_video_fps(args.video_file)
        log.info("📹 FPS: %.1f", fps)

    if not total_seconds:
        log.error("❌ Can't determine video duration. Use --total-seconds")
        return

    # Create output dir
    os.makedirs(args.output, exist_ok=True)

    # Calculate segments
    segments = []
    current = 0
    seg_num = 1
    while current < total_seconds:
        start_frame = int(current * fps)
        end_frame = int(min(current + SEGMENT_DURATION, total_seconds) * fps)
        if end_frame <= start_frame:
            break
        segments.append((seg_num, start_frame, end_frame))
        current += SEGMENT_DURATION
        seg_num += 1

    log.info("📋 Total: %.1fs → %d segments of %ds each", total_seconds, len(segments), SEGMENT_DURATION)
    log.info("🚀 Parallel: %d concurrent", min(5, len(segments)))
    log.info("─" * 50)

    # Connect to extension
    bridge = ExtensionBridge()
    await bridge.start()

    if not await bridge.wait_for_extension(timeout=30):
        return

    # Process segments in parallel (max 5 concurrent)
    semaphore = asyncio.Semaphore(5)
    results = [None] * len(segments)

    async def process_segment(idx, seg_num, start_frame, end_frame):
        async with semaphore:
            out = await edit_segment(
                bridge, args.prompt, aspect, args.project_id,
                args.media_id, start_frame, end_frame, seg_num, args.output
            )
            results[idx] = out

    tasks = []
    for idx, (seg_num, start_frame, end_frame) in enumerate(segments):
        task = asyncio.create_task(
            process_segment(idx, seg_num, start_frame, end_frame)
        )
        tasks.append(task)

    await asyncio.gather(*tasks)
    await bridge.close()

    # Collect saved files (in order)
    saved = [r for r in results if r]

    # Summary
    log.info("─" * 50)
    log.info("🎉 Done! %d/%d segments saved to %s/", len(saved), len(segments), args.output)
    for f in saved:
        log.info("   ✅ %s", os.path.basename(f))

    # Try to merge with ffmpeg
    if len(saved) > 1 and args.merge:
        merge_path = os.path.join(args.output, "merged_output.mp4")
        log.info("🔗 Merging %d segments...", len(saved))
        try:
            concat_file = os.path.join(args.output, "concat.txt")
            with open(concat_file, "w") as f:
                for s in saved:
                    f.write(f"file '{os.path.abspath(s)}'\n")
            subprocess.run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", concat_file, "-c", "copy", merge_path
            ], capture_output=True)
            os.remove(concat_file)
            log.info("✅ Merged: %s", merge_path)
            if sys.platform == "darwin":
                os.system(f'open "{merge_path}"')
        except Exception as e:
            log.warning("⚠️ Merge failed (ffmpeg needed): %s", e)


def main():
    parser = argparse.ArgumentParser(description="Omni Flash — Full Video Editor")
    parser.add_argument("prompt", help="Edit prompt (applied to all segments)")
    parser.add_argument("--media-id", "-m", required=True,
                        help="Flow media ID of uploaded video")
    parser.add_argument("--video-file", "-v",
                        help="Local video file (for auto-detecting duration/fps)")
    parser.add_argument("--total-seconds", "-t", type=float,
                        help="Total video duration in seconds (if no local file)")
    parser.add_argument("--output", "-o", default="output", help="Output directory")
    parser.add_argument("--aspect", "-a", choices=["portrait", "landscape"], default="portrait")
    parser.add_argument("--merge", action="store_true", help="Merge segments with ffmpeg")
    parser.add_argument("--project-id", "-p", default=DEFAULT_PROJECT)
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
