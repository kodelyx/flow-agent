#!/usr/bin/env python3
"""CLI — Parallel Batch Image and Video Generator.

Usage:
    # Batch Images:
    flow batch prompts.txt -o output_images/ -c 4 -s 2.0 -a landscape
    
    # Batch Videos:
    flow batch prompts.txt --type video -d 10 -o output_videos/ -c 2 -a landscape
"""

import os
import sys
import time
import json
import uuid
import argparse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flow_engine.config import MAX_CONCURRENT_REQUESTS, REQUEST_MIN_INTERVAL


def generate_single_item(
    item_type: str,
    prompt: str,
    out_file: str,
    aspect: str = "landscape",
    duration: int = 10,
    model: str = "narwhal",
    max_retries: int = 2,
    retry_delay: float = 5.0,
    base_url: str = "http://127.0.0.1:8001",
    api_key: str = "flow-local",
):
    """Generate and save a single image or video with caching, async polling, and retry."""
    name = os.path.splitext(os.path.basename(out_file))[0]
    expected_ext = ".mp4" if item_type == "video" else ".png"
    
    if not out_file.endswith(expected_ext):
        out_file = f"{os.path.splitext(out_file)[0]}{expected_ext}"

    # Skip if valid media already exists (>10KB)
    if os.path.exists(out_file) and os.path.getsize(out_file) > 10240:
        print(f"[EXISTS] {name} already exists ({os.path.getsize(out_file)} bytes). Skipping.", flush=True)
        return True, name, out_file

    sizes = {
        "landscape": "1792x1024",
        "4x3": "1365x1024",
        "square": "1024x1024",
        "3x4": "1024x1365",
        "portrait": "1024x1792",
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    for attempt in range(1, max_retries + 1):
        print(f"[{name}] Starting {item_type} generation (Attempt {attempt}/{max_retries})...", flush=True)
        start_t = time.time()
        idempotency_key = uuid.uuid4().hex
        headers["Idempotency-Key"] = idempotency_key
        client_id = "worker"

        try:
            if item_type == "video":
                # --- Video Generation Flow ---
                payload = {
                    "prompt": prompt,
                    "aspect": aspect if aspect in ["portrait", "landscape"] else "landscape",
                    "duration": duration,
                    "n": 1,
                }
                api_url = f"{base_url}/v1/videos/generations"
                req = urllib.request.Request(
                    api_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                # Video returns a job or direct result
                job_id = data.get("id") or (data.get("data", [{}])[0].get("id") if data.get("data") else None)
                video_url = None
                client_id = data.get("client_id") or client_id

                if data.get("data") and data["data"][0].get("url"):
                    video_url = data["data"][0]["url"]
                    client_id = data["data"][0].get("client_id") or client_id
                elif job_id:
                    # Poll for video completion (up to 7 minutes)
                    poll_url = f"{base_url}/v1/jobs/{job_id}"
                    deadline = time.time() + 420
                    while time.time() < deadline:
                        time.sleep(8)
                        poll_req = urllib.request.Request(poll_url, headers=headers)
                        with urllib.request.urlopen(poll_req, timeout=30) as p_resp:
                            job_data = json.loads(p_resp.read().decode("utf-8"))

                        status = job_data.get("status")
                        if status == "completed":
                            items = job_data.get("result", {}).get("data", [])
                            if items and items[0].get("url"):
                                video_url = items[0]["url"]
                                client_id = items[0].get("client_id") or job_data.get("result", {}).get("client_id") or client_id
                            break
                        elif status in ("failed", "error"):
                            err_msg = job_data.get("error", "Video job failed")
                            raise ValueError(err_msg)

                if not video_url:
                    raise ValueError("Failed to retrieve video URL from job")

                # Download video MP4
                dl_req = urllib.request.Request(video_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(dl_req, timeout=180) as dl_resp:
                    media_bytes = dl_resp.read()

            else:
                # --- Image Generation Flow ---
                payload = {
                    "prompt": prompt,
                    "model": model,
                    "n": 1,
                    "size": sizes.get(aspect, "1792x1024"),
                    "response_format": "url",
                }
                api_url = f"{base_url}/v1/images/generations"
                req = urllib.request.Request(
                    api_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                items = data.get("data", [])
                if not items or not items[0].get("url"):
                    raise ValueError("No image URL returned by backend")

                img_url = items[0]["url"]
                client_id = items[0].get("client_id") or "worker"
                dl_req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(dl_req, timeout=60) as dl_resp:
                    media_bytes = dl_resp.read()

            os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
            with open(out_file, "wb") as f:
                f.write(media_bytes)

            elapsed = round(time.time() - start_t, 1)
            if os.path.exists(out_file) and os.path.getsize(out_file) > 10240:
                print(f"[SUCCESS] {name} finished in {elapsed}s via [{client_id}] -> {out_file} ({os.path.getsize(out_file)} bytes)", flush=True)
                return True, name, out_file, client_id
            else:
                raise ValueError("Downloaded media file is invalid or too small")

        except Exception as exc:
            elapsed = round(time.time() - start_t, 1)
            err_detail = ""
            if hasattr(exc, "read"):
                try:
                    err_detail = f" ({exc.read().decode('utf-8', errors='ignore')})"
                except Exception:
                    pass
            print(f"[WARN] {name} attempt {attempt} failed ({elapsed}s): {exc}{err_detail}", flush=True)
            if attempt < max_retries:
                time.sleep(retry_delay * attempt)

    print(f"[ERROR] {name} failed after {max_retries} attempts.", flush=True)
    return False, name, out_file, "failed"


def run_batch(
    prompts_list,
    item_type: str = "image",
    out_dir: str = "output_media",
    max_workers: int = 4,
    stagger_gap: float = 2.0,
    aspect: str = "landscape",
    duration: int = 10,
    model: str = "narwhal",
    max_retries: int = 2,
):
    """Run parallel batch generation with stagger gap for images or videos."""
    os.makedirs(out_dir, exist_ok=True)
    ext = ".mp4" if item_type == "video" else ".png"

    print(f"=== Flow Agent Parallel Batch {item_type.upper()} Generator ===")
    print(f"Total {item_type.capitalize()}s: {len(prompts_list)} | Concurrency: {max_workers} | Stagger Gap: {stagger_gap}s")
    print(f"Output Directory: {os.path.abspath(out_dir)}\n", flush=True)

    results = {}
    worker_counts = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for idx, item in enumerate(prompts_list):
            if isinstance(item, tuple) or isinstance(item, list):
                name, prompt = item[0], item[1]
            else:
                name, prompt = f"{item_type}_{idx+1:03d}", str(item)

            out_file = os.path.join(out_dir, f"{name}{ext}")
            futures[executor.submit(
                generate_single_item,
                item_type,
                prompt,
                out_file,
                aspect=aspect,
                duration=duration,
                model=model,
                max_retries=max_retries,
            )] = name

            if stagger_gap > 0 and idx < len(prompts_list) - 1:
                time.sleep(stagger_gap)

        for future in as_completed(futures):
            success, name, path, client_id = future.result()
            results[name] = success
            if success and client_id != "failed":
                worker_counts[client_id] = worker_counts.get(client_id, 0) + 1

    total_done = sum(1 for s in results.values() if s)
    print(f"\n=== Completed: {total_done}/{len(prompts_list)} {item_type.capitalize()}s Generated Successfully ===", flush=True)
    print("\n--- Worker Distribution Breakdown ---")
    for cid, count in sorted(worker_counts.items(), key=lambda x: -x[1]):
        print(f"  • Worker [{cid}]: {count} generated")
    print("--------------------------------------\n", flush=True)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="flow batch",
        description="Generate batch images or videos in parallel using Flow Agent"
    )
    parser.add_argument("file", nargs="?", help="Path to text file containing prompts (one per line)")
    parser.add_argument("-t", "--type", choices=["image", "video"], default="image", help="Media type: image or video (default: image)")
    parser.add_argument("-o", "--output", default=None, help="Output directory (default: output_images or output_videos)")
    parser.add_argument("-c", "--concurrency", type=int, default=int(os.environ.get("MAX_CONCURRENT_REQUESTS", "4")), help="Parallel concurrency (default: 4)")
    parser.add_argument("-s", "--stagger", type=float, default=float(os.environ.get("REQUEST_MIN_INTERVAL", "2.0")), help="Stagger delay in seconds (default: 2.0s)")
    parser.add_argument("-a", "--aspect", default="landscape", choices=["landscape", "portrait", "square", "4x3", "3x4"], help="Aspect ratio")
    parser.add_argument("-d", "--duration", type=int, choices=[4, 6, 8, 10], default=10, help="Video duration in seconds (for video mode, default: 10)")
    parser.add_argument("-m", "--model", default="narwhal", help="Model name (default: narwhal)")
    parser.add_argument("--retries", type=int, default=2, help="Retry attempts per prompt (default: 2)")

    args = parser.parse_args(argv)

    out_dir = args.output
    if not out_dir:
        out_dir = "output_videos" if args.type == "video" else "output_images"

    prompts = []
    if args.file:
        if os.path.isfile(args.file):
            with open(args.file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        prompts.append(line)
        else:
            print(f"Error: Prompt file not found: {args.file}", file=sys.stderr)
            sys.exit(1)
    elif not sys.stdin.isatty():
        for line in sys.stdin:
            line = line.strip()
            if line and not line.startswith("#"):
                prompts.append(line)

    if not prompts:
        if args.type == "video":
            prompts = [
                ("vid_01", "Cinematic drone shot soaring through a futuristic cyberpunk city in rain"),
                ("vid_02", "Golden hour sunset over majestic snowy alpine mountain peaks"),
            ]
        else:
            prompts = [
                ("img_01", "Cyberpunk neon city street in heavy rain with reflective puddles and flying cars"),
                ("img_02", "Majestic snow-capped mountain peak at golden hour sunset with a crystalline alpine lake"),
            ]

    run_batch(
        prompts,
        item_type=args.type,
        out_dir=out_dir,
        max_workers=args.concurrency,
        stagger_gap=args.stagger,
        aspect=args.aspect,
        duration=args.duration,
        model=args.model,
        max_retries=args.retries,
    )


if __name__ == "__main__":
    main()
