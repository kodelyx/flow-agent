#!/usr/bin/env python3
"""CLI — Generate image from text prompt (T2I).

Usage:
    python -m flow_server.image "A cat wearing sunglasses on a beach"
    python -m flow_server.image "Dragon in cyberpunk city" --aspect landscape --count 4
    python -m flow_server.image "Logo design" --aspect square -o logo.png
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flow_engine import ExtensionBridge, DEFAULT_PROJECT
from flow_engine.config import OUTPUT_DIR
from flow_engine.generators.t2i import generate_image, download_image, IMAGE_ASPECTS
from flow_server.history import MediaNotFoundError
from flow_server.media_history import record_local_media, resolve_media_reference
from flow_server.media_types import ensure_correct_extension


async def run(args):
    aspect = args.aspect

    bridge = ExtensionBridge()
    await bridge.start()

    if not await bridge.wait_for_extension(timeout=30):
        return

    # Handle ref images: auto-upload local files
    ref_ids = []
    if args.ref:
        from flow_engine.generators.i2v import upload_image
        for ref in args.ref:
            if os.path.exists(ref):
                print(f"Uploading reference: {ref}")
                mid = await upload_image(bridge, ref)
                if mid:
                    ref_ids.append(mid)
                    print(f"   media_id={mid[:12]}...")
            else:
                try:
                    ref_ids.append(
                        await resolve_media_reference(
                            ref,
                            bridge,
                            expected_type="image",
                            project_id=args.project_id,
                        )
                    )
                except (MediaNotFoundError, ValueError, RuntimeError) as exc:
                    print(f"Reference media not found: {exc}", file=sys.stderr)
                    await bridge.close()
                    return

    results = await generate_image(
        bridge, args.prompt, aspect, args.project_id,
        count=args.count, ref_media_ids=ref_ids or None,
        model=args.model
    )

    if not results:
        await bridge.close()
        return

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    for i, r in enumerate(results):
        if not r.get("image_url"):
            print(f"Image {i+1}: no URL")
            continue

        if len(results) == 1:
            out_path = args.output
        else:
            base, ext = os.path.splitext(args.output)
            out_path = f"{base}_{i+1}{ext}"

        if await download_image(bridge, r["image_url"], out_path):
            if not args.output_explicit:
                out_path = ensure_correct_extension(out_path)
            record_local_media(
                out_path,
                media_id=r.get("media_id"),
                project_id=args.project_id,
                prompt=args.prompt,
                source="generated",
            )
            print(f"Done! {out_path}")

    await bridge.close()


def main():
    parser = argparse.ArgumentParser(description="Flow Agent — Image Generator")
    parser.add_argument("prompt", help="Text prompt for image")
    parser.add_argument("--output", "-o", default=None, help="Output file")
    parser.add_argument("--aspect", "-a",
                        choices=list(IMAGE_ASPECTS.keys()),
                        default="portrait",
                        help="Aspect ratio")
    parser.add_argument("--count", "-c", type=int, choices=[1, 2, 3, 4], default=1,
                        help="Generate 1-4 variations")
    parser.add_argument("--ref", "-r", nargs="+", metavar="IMAGE",
                        help="Reference image(s): file path or media_id")
    parser.add_argument("--project-id", "-p", default=DEFAULT_PROJECT)
    parser.add_argument("--model", "-m", default="gem_pix_2",
                        help="Image model to use (harbor_seal/lite, narwhal/standard, gem_pix_2/pro)")
    args = parser.parse_args()
    args.output_explicit = args.output is not None
    args.output = os.path.abspath(os.path.expanduser(args.output or os.path.join(OUTPUT_DIR, "image.png")))
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
