#!/usr/bin/env python3
"""CLI — Generate video from text prompt (T2V) or edit existing video (V2V).

Usage:
    python -m flow_server.generate "A dragon breathing fire"
    python -m flow_server.generate "A dragon breathing fire" --aspect landscape -o dragon.mp4
    python -m flow_server.generate "Make it anime" --edit MEDIA_ID
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flow_engine import (
    ExtensionBridge, generate_video, edit_video,
    poll_status, download_video, ASPECTS, DEFAULT_PROJECT,
)
from flow_engine.config import OUTPUT_DIR
from flow_engine.generators.i2v import upload_image, generate_video_i2v, generate_video_fl, generate_video_r2v
from flow_server.history import MediaNotFoundError
from flow_server.media_history import record_local_media, resolve_media_reference
from flow_server.media_types import ensure_correct_extension


async def run(args):
    aspect = ASPECTS.get(args.aspect, "VIDEO_ASPECT_RATIO_PORTRAIT")

    bridge = ExtensionBridge()
    await bridge.start()

    if not await bridge.wait_for_extension(timeout=30):
        return

    # Auto-upload local image files
    async def resolve_image(path_or_id):
        if os.path.exists(path_or_id):
            mid = await upload_image(bridge, path_or_id)
            if mid:
                print(f"Uploaded: {path_or_id} -> {mid[:12]}...")
            return mid
        try:
            return await resolve_media_reference(
                path_or_id,
                bridge,
                expected_type="image",
                project_id=args.project_id,
            )
        except (MediaNotFoundError, ValueError, RuntimeError) as exc:
            print(f"Reference media not found: {exc}", file=sys.stderr)
            return None

    if args.start and args.end:
        # First+Last frame mode
        start_id = await resolve_image(args.start)
        end_id = await resolve_image(args.end)
        if not start_id or not end_id:
            await bridge.close()
            return
        media_ids = await generate_video_fl(bridge, args.prompt, aspect, args.project_id,
                                             start_image_id=start_id, end_image_id=end_id,
                                             duration=args.duration)
    elif args.start:
        # I2V mode (start image only)
        start_id = await resolve_image(args.start)
        if not start_id:
            await bridge.close()
            return
        media_ids = await generate_video_i2v(bridge, args.prompt, aspect, args.project_id,
                                              image_media_id=start_id, duration=args.duration)
    elif args.ref:
        # Reference images mode
        ref_ids = []
        for r in args.ref:
            mid = await resolve_image(r)
            if mid:
                ref_ids.append(mid)
        if not ref_ids:
            await bridge.close()
            return
        media_ids = await generate_video_r2v(bridge, args.prompt, aspect, args.project_id,
                                              ref_media_ids=ref_ids, duration=args.duration)
    elif args.edit:
        try:
            edit_id = await resolve_media_reference(
                args.edit,
                bridge,
                expected_type="video",
                project_id=args.project_id,
            )
        except (MediaNotFoundError, ValueError, RuntimeError) as exc:
            print(f"Edit media not found: {exc}", file=sys.stderr)
            await bridge.close()
            return
        media_ids = await edit_video(bridge, args.prompt, aspect, args.project_id,
                                     video_media_id=edit_id, duration=args.duration)
    else:
        media_ids = await generate_video(bridge, args.prompt, aspect, args.project_id,
                                         duration=args.duration, count=args.count)

    if not media_ids:
        await bridge.close()
        return

    for i, media_id in enumerate(media_ids):
        label = f"[{i+1}/{len(media_ids)}] " if len(media_ids) > 1 else ""
        print(f"{label}Polling {media_id[:12]}...")
        if not await poll_status(bridge, media_id, args.project_id):
            continue

        if len(media_ids) == 1:
            out_path = args.output
        else:
            base, ext = os.path.splitext(args.output)
            out_path = f"{base}_{i+1}{ext}"

        # Setup temp dir for download
        out_dir = os.path.dirname(out_path) or "."
        temp_dir = os.path.join(out_dir, ".temp")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, os.path.basename(out_path))

        if await download_video(bridge, media_id, temp_path):
            os.replace(temp_path, out_path)
            if not args.output_explicit:
                out_path = ensure_correct_extension(out_path)
            record_local_media(
                out_path,
                media_id=media_id,
                project_id=args.project_id,
                prompt=args.prompt,
                source="generated",
            )

            # Cleanup empty .temp dir
            try:
                os.rmdir(temp_dir)
            except OSError:
                pass

            print(f"Done! {out_path}")

    await bridge.close()


def main():
    parser = argparse.ArgumentParser(description="Flow Engine — Video Generator")
    parser.add_argument("prompt", help="Text prompt for video")
    parser.add_argument("--output", "-o", default=None, help="Output file")
    parser.add_argument("--aspect", "-a", choices=["portrait", "landscape"], default="portrait")
    parser.add_argument("--duration", "-d", type=int, choices=[4, 6, 8, 10], default=10)
    parser.add_argument("--count", "-c", type=int, choices=[1, 2, 3, 4], default=1)
    parser.add_argument("--edit", "-e", metavar="MEDIA_ID",
                        help="Edit existing video (V2V mode)")
    parser.add_argument("--start", "-s", metavar="IMAGE",
                        help="Start frame image (file path or media_id)")
    parser.add_argument("--end", metavar="IMAGE",
                        help="End frame image (use with --start for FL mode)")
    parser.add_argument("--ref", "-r", nargs="+", metavar="IMAGE",
                        help="Reference images for R2V mode")
    parser.add_argument("--project-id", "-p", default=DEFAULT_PROJECT)
    args = parser.parse_args()
    args.output_explicit = args.output is not None
    args.output = os.path.abspath(os.path.expanduser(args.output or os.path.join(OUTPUT_DIR, "omni_output.mp4")))
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
