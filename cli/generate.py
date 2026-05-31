#!/usr/bin/env python3
"""CLI — Generate video from text prompt (T2V) or edit existing video (V2V).

Usage:
    python -m cli.generate "A dragon breathing fire"
    python -m cli.generate "A dragon breathing fire" --aspect landscape -o dragon.mp4
    python -m cli.generate "Make it anime" --edit MEDIA_ID
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omniflash import (
    ExtensionBridge, generate_video, edit_video,
    poll_status, download_video, ASPECTS, DEFAULT_PROJECT,
)


async def run(args):
    aspect = ASPECTS.get(args.aspect, "VIDEO_ASPECT_RATIO_PORTRAIT")

    bridge = ExtensionBridge()
    await bridge.start()

    if not await bridge.wait_for_extension(timeout=30):
        return

    if args.edit:
        media_ids = await edit_video(bridge, args.prompt, aspect, args.project_id,
                                     video_media_id=args.edit, duration=args.duration)
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

        if await download_video(bridge, media_id, out_path):
            # Auto-remove watermark unless --no-clean
            if not args.no_clean:
                try:
                    from omniflash.watermark import remove_watermark_video
                    clean_path = remove_watermark_video(out_path)
                    # Replace original with clean version
                    os.replace(clean_path, out_path)
                    print(f"🧹 Watermark removed!")
                except Exception as e:
                    print(f"⚠️  Watermark removal failed: {e}")
            print(f"🎉 Done! {out_path}")
            if sys.platform == "darwin":
                os.system(f'open "{out_path}"')

    await bridge.close()


def main():
    parser = argparse.ArgumentParser(description="Omni Flash — Video Generator")
    parser.add_argument("prompt", help="Text prompt for video")
    parser.add_argument("--output", "-o", default="omni_output.mp4", help="Output file")
    parser.add_argument("--aspect", "-a", choices=["portrait", "landscape"], default="portrait")
    parser.add_argument("--duration", "-d", type=int, choices=[4, 6, 8, 10], default=10)
    parser.add_argument("--count", "-c", type=int, choices=[1, 2, 3, 4], default=1)
    parser.add_argument("--edit", "-e", metavar="MEDIA_ID",
                        help="Edit existing video (V2V mode)")
    parser.add_argument("--project-id", "-p", default=DEFAULT_PROJECT)
    parser.add_argument("--no-clean", action="store_true",
                        help="Skip automatic watermark removal")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
