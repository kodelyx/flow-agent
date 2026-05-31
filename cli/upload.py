#!/usr/bin/env python3
"""CLI — Upload video or batch upload directory.

Usage:
    python -m cli.upload video.mp4
    python -m cli.upload chunks/ --batch
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omniflash.upload import upload_video


async def run(args):
    if args.batch or os.path.isdir(args.path):
        # Batch upload all mp4 in directory
        directory = args.path
        chunks = sorted([f for f in os.listdir(directory) if f.endswith(".mp4")])
        if not chunks:
            print(f"❌ No .mp4 files found in {directory}")
            return
        print(f"📁 Found {len(chunks)} videos in {directory}")
        for i, chunk in enumerate(chunks, 1):
            path = os.path.join(directory, chunk)
            print(f"\n{'─' * 50}")
            print(f"[{i}/{len(chunks)}] {chunk}")
            try:
                await upload_video(path, args.project_id)
            except Exception as e:
                print(f"❌ {chunk} failed: {e}")
    else:
        # Single file upload
        result = await upload_video(args.path, args.project_id)
        print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Omni Flash — Upload Video")
    parser.add_argument("path", help="Video file or directory of videos")
    parser.add_argument("--batch", "-b", action="store_true",
                        help="Batch upload all .mp4 in directory")
    parser.add_argument("--project-id", "-p",
                        default="ff92d5cc-8a03-41d2-b59e-e0774d17bcf6")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
