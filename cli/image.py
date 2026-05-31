#!/usr/bin/env python3
"""CLI — Generate image from text prompt (T2I).

Usage:
    python -m cli.image "A cat wearing sunglasses on a beach"
    python -m cli.image "Dragon in cyberpunk city" --aspect landscape --count 4
    python -m cli.image "Logo design" --aspect square -o logo.png
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omniflash import ExtensionBridge, DEFAULT_PROJECT
from omniflash.generators.t2i import generate_image, download_image, IMAGE_ASPECTS


async def run(args):
    aspect = args.aspect

    bridge = ExtensionBridge()
    await bridge.start()

    if not await bridge.wait_for_extension(timeout=30):
        return

    results = await generate_image(
        bridge, args.prompt, aspect, args.project_id, count=args.count
    )

    if not results:
        await bridge.close()
        return

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    for i, r in enumerate(results):
        if not r.get("image_url"):
            print(f"⚠️  Image {i+1}: no URL")
            continue

        if len(results) == 1:
            out_path = args.output
        else:
            base, ext = os.path.splitext(args.output)
            out_path = f"{base}_{i+1}{ext}"

        if await download_image(bridge, r["image_url"], out_path):
            print(f"🎉 Done! {out_path}")
            if sys.platform == "darwin":
                os.system(f'open "{out_path}"')

    await bridge.close()


def main():
    parser = argparse.ArgumentParser(description="Flow Agent — Image Generator")
    parser.add_argument("prompt", help="Text prompt for image")
    parser.add_argument("--output", "-o", default="output/image.png", help="Output file")
    parser.add_argument("--aspect", "-a",
                        choices=list(IMAGE_ASPECTS.keys()),
                        default="portrait",
                        help="Aspect ratio")
    parser.add_argument("--count", "-c", type=int, choices=[1, 2, 3, 4], default=1,
                        help="Generate 1-4 variations")
    parser.add_argument("--project-id", "-p", default=DEFAULT_PROJECT)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
