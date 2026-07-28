#!/usr/bin/env python3
"""Unified Flow CLI.

The single ``flow`` executable starts the backend by default and also exposes
MCP, image, video, edit, upload, and developer commands as subcommands.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

SHUTDOWN_TIMEOUT = int(os.environ.get("SHUTDOWN_TIMEOUT", "5"))


def _api_base() -> str:
    host = os.environ.get("OPENAI_API_HOST", "127.0.0.1")
    port = os.environ.get("OPENAI_API_PORT", "8001")
    return f"http://{host}:{port}"


def _ensure_backend():
    url = f"{_api_base()}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status == 200:
                return True
    except Exception:
        pass

    import subprocess
    import time

    print("[flow] Backend not running. Auto-starting backend server...")
    try:
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            command = [sys.executable, os.path.abspath(__file__)]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(15):
            time.sleep(1)
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        print("[flow] Backend server auto-started successfully!")
                        return True
            except Exception:
                pass
    except Exception as error:
        print(f"[flow] Auto-start backend failed: {error}", file=sys.stderr)
    return False


def _forward(module_main, argv):
    _ensure_backend()
    saved = sys.argv
    sys.argv = [saved[0]] + argv
    try:
        module_main()
    finally:
        sys.argv = saved


def run_backend(argv):
    parser = argparse.ArgumentParser(
        prog="flow",
        description="Start the Flow backend, MCP-over-SSE, and extension bridge.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("OPENAI_API_HOST", "127.0.0.1"),
        help="Bind address (use 0.0.0.0 to expose on the network).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("OPENAI_API_PORT", "8001")),
        help="Port (default: 8001).",
    )
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")
    args = parser.parse_args(argv)

    import uvicorn

    print(f"Flow starting on http://{args.host}:{args.port}")
    print("Waiting for the Chrome extension (open Google Flow in Chrome to connect).")
    uvicorn.run(
        "flow_server.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        access_log=False,
        timeout_graceful_shutdown=SHUTDOWN_TIMEOUT,
    )


def run_mcp(argv):
    parser = argparse.ArgumentParser(prog="flow mcp", description="Run Flow's MCP stdio transport.")
    parser.parse_args(argv)
    from flow_server.mcp_server import main as mcp_main
    mcp_main()


def cmd_image(argv):
    parser = argparse.ArgumentParser(prog="flow image", description="Generate images through the Flow backend.")
    parser.add_argument("prompt", help="Text prompt for image")
    parser.add_argument("--output", "-o", default="output/image.png", help="Output file")
    parser.add_argument(
        "--aspect",
        "-a",
        choices=["landscape", "4x3", "square", "3x4", "portrait"],
        default="portrait",
    )
    parser.add_argument("--count", "-c", type=int, choices=[1, 2, 3, 4], default=1)
    parser.add_argument("--ref", "-r", nargs="+", metavar="IMAGE", help="Reference path or media ID")
    parser.add_argument("--model", "-m", default="gem_pix_2")
    args = parser.parse_args(argv)

    _ensure_backend()
    sizes = {
        "landscape": "1792x1024",
        "4x3": "1365x1024",
        "square": "1024x1024",
        "3x4": "1024x1365",
        "portrait": "1024x1792",
    }
    payload = {
        "prompt": args.prompt,
        "model": args.model,
        "n": args.count,
        "size": sizes[args.aspect],
        "response_format": "url",
    }

    media_ids = []
    local_refs = []
    for ref in args.ref or []:
        if os.path.isfile(ref):
            local_refs.append(ref)
        else:
            media_ids.append(ref)
    if len(local_refs) > 1:
        parser.error("flow image currently accepts at most one local reference file")
    if local_refs:
        with open(local_refs[0], "rb") as reference_file:
            payload["image_base64"] = base64.b64encode(reference_file.read()).decode("ascii")
    if media_ids:
        payload["ref_media_ids"] = media_ids[:10]

    request = urllib.request.Request(
        f"{_api_base()}/v1/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        parser.error(f"generation failed ({error.code}): {detail}")

    outputs = result.get("data", [])
    if not outputs:
        parser.error("Flow returned no images")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    for index, item in enumerate(outputs, start=1):
        image_url = item.get("url")
        if not image_url:
            continue
        if len(outputs) == 1:
            output_path = args.output
        else:
            stem, extension = os.path.splitext(args.output)
            output_path = f"{stem}_{index}{extension}"
        with urllib.request.urlopen(image_url, timeout=120) as response, open(output_path, "wb") as output_file:
            output_file.write(response.read())
        print(f"Done! {output_path}")
        if item.get("media_id"):
            print(f"media_id={item['media_id']}")


def cmd_video(argv):
    from flow_server.generate import main as video_main
    _forward(video_main, argv)


def cmd_edit(argv):
    from flow_server.edit import main as edit_main
    _forward(edit_main, argv)


def cmd_upload(argv):
    from flow_server.upload import main as upload_main
    _forward(upload_main, argv)


def cmd_sniff(argv):
    from flow_server.sniff import main as sniff_main
    _forward(sniff_main, argv)


def cmd_credits(argv):
    parser = argparse.ArgumentParser(prog="flow credits", description="Show Flow credits.")
    parser.parse_args(argv)
    _ensure_backend()
    try:
        with urllib.request.urlopen(f"{_api_base()}/v1/credits", timeout=15) as response:
            data = json.load(response)
        if "total_credits" in data:
            print(f"Total Google Flow credits: {data['total_credits']} ({data.get('client_count', 0)} clients)")
        else:
            print(f"Remaining Google Flow credits: {data.get('credits', data)}")
    except urllib.error.URLError as error:
        _backend_down(error)


def cmd_status(argv):
    parser = argparse.ArgumentParser(prog="flow status", description="Check backend health.")
    parser.parse_args(argv)
    _ensure_backend()
    try:
        with urllib.request.urlopen(f"{_api_base()}/health", timeout=5) as response:
            data = json.load(response)
        print(f"Backend: up ({_api_base()})")
        print(f"  status:              {data.get('status')}")
        print(f"  extension_connected: {data.get('extension_connected')}")
        print(f"  has_flow_key:        {data.get('has_flow_key')}")
    except urllib.error.URLError as error:
        _backend_down(error)


def _backend_down(error):
    print(f"Could not reach the backend at {_api_base()}.", file=sys.stderr)
    print("Start it first with `flow` and connect the Chrome extension.", file=sys.stderr)
    print(f"Details: {error}", file=sys.stderr)
    raise SystemExit(1)


COMMANDS = {
    "mcp": run_mcp,
    "image": cmd_image,
    "video": cmd_video,
    "edit": cmd_edit,
    "upload": cmd_upload,
    "sniff": cmd_sniff,
    "credits": cmd_credits,
    "status": cmd_status,
}


def _usage():
    print("flow — Flow Agent CLI\n")
    print("Usage:")
    print("  flow [--host HOST] [--port PORT]  Start the backend")
    print("  flow <command> [args...]\n")
    print("Commands:")
    print("  mcp        Run MCP stdio mode")
    print("  image      Generate images")
    print("  video      Generate videos")
    print("  edit       Edit a video")
    print("  upload     Upload media")
    print("  sniff      Capture Flow API requests")
    print("  credits    Show Flow credits")
    print("  status     Show backend health")


def main():
    if len(sys.argv) < 2:
        run_backend([])
        return

    first = sys.argv[1]
    if first in ("-h", "--help", "help"):
        _usage()
        return
    if first.startswith("-"):
        run_backend(sys.argv[1:])
        return

    handler = COMMANDS.get(first)
    if handler is None:
        print(f"Unknown command: {first}\n", file=sys.stderr)
        _usage()
        raise SystemExit(2)
    handler(sys.argv[2:])


if __name__ == "__main__":
    main()
