#!/usr/bin/env python3
"""flow — unified CLI for Flow Agent (Google Flow image/video generation).

Replaces the old Docker workflow. Install with `uv tool install .` or
`pipx install .`, then:

    flow serve                     # start the API + MCP backend (Chrome extension bridge)
    flow video "a dragon flying"   # text-to-video
    flow image "neon city" -c 4    # text-to-image
    flow edit "make it anime" -m MEDIA_ID
    flow upload clip.mp4
    flow sniff                     # dev: capture Flow API requests
    flow credits                   # remaining Google Flow credits
    flow status                    # is the backend running?

Every subcommand except `serve`/`credits`/`status` forwards its arguments
straight to the existing `cli.*` entry points, so there is a single source
of truth for the generation logic.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def _api_base() -> str:
    host = os.environ.get("OPENAI_API_HOST", "127.0.0.1")
    port = os.environ.get("OPENAI_API_PORT", "8001")
    return f"http://{host}:{port}"


def _forward(module_main, argv):
    """Run an existing cli.* main() with a rewritten argv."""
    saved = sys.argv
    sys.argv = [saved[0]] + argv
    try:
        module_main()
    finally:
        sys.argv = saved


def cmd_serve(argv):
    parser = argparse.ArgumentParser(prog="flow serve", description="Start the Flow Agent backend (API + MCP + extension bridge).")
    parser.add_argument("--host", default=os.environ.get("OPENAI_API_HOST", "127.0.0.1"),
                        help="Bind address (use 0.0.0.0 to expose on the network).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPENAI_API_PORT", "8001")),
                        help="Port (default: 8001).")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev).")
    args = parser.parse_args(argv)

    import uvicorn
    print(f"Flow Agent starting on http://{args.host}:{args.port}")
    print("Waiting for the Chrome extension (open Google Flow in Chrome to connect).")
    uvicorn.run("cli.api:app", host=args.host, port=args.port,
                reload=args.reload, access_log=False)


def cmd_credits(argv):
    parser = argparse.ArgumentParser(prog="flow credits", description="Show remaining Google Flow credits.")
    parser.parse_args(argv)
    url = f"{_api_base()}/v1/credits"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        credits = data.get("credits", data)
        print(f"Remaining Google Flow credits: {credits}")
    except urllib.error.URLError as e:
        _backend_down(e)


def cmd_status(argv):
    parser = argparse.ArgumentParser(prog="flow status", description="Check whether the backend is running.")
    parser.parse_args(argv)
    url = f"{_api_base()}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        print(f"Backend: up ({_api_base()})")
        print(f"  status:              {data.get('status')}")
        print(f"  extension_connected: {data.get('extension_connected')}")
        print(f"  has_flow_key:        {data.get('has_flow_key')}")
    except urllib.error.URLError:
        print(f"Backend: down. Start it with `flow serve`.")
        sys.exit(1)


def _backend_down(err):
    print(f"Could not reach the backend at {_api_base()}.", file=sys.stderr)
    print("Start it first with `flow serve` (and connect the Chrome extension).", file=sys.stderr)
    print(f"Details: {err}", file=sys.stderr)
    sys.exit(1)


def cmd_video(argv):
    from cli.generate import main
    _forward(main, argv)


def cmd_image(argv):
    from cli.image import main
    _forward(main, argv)


def cmd_edit(argv):
    from cli.edit import main
    _forward(main, argv)


def cmd_upload(argv):
    from cli.upload import main
    _forward(main, argv)


def cmd_sniff(argv):
    from cli.sniff import main
    _forward(main, argv)


COMMANDS = {
    "serve": cmd_serve,
    "video": cmd_video,
    "image": cmd_image,
    "edit": cmd_edit,
    "upload": cmd_upload,
    "sniff": cmd_sniff,
    "credits": cmd_credits,
    "status": cmd_status,
}


def _usage():
    print("flow — Flow Agent CLI\n")
    print("Usage: flow <command> [args...]\n")
    print("Commands:")
    print("  serve      Start the API + MCP backend (Chrome extension bridge)")
    print("  video      Generate a video from a text prompt (text/image-to-video)")
    print("  image      Generate an image from a text prompt")
    print("  edit       Edit an existing Flow video (V2V, segmented)")
    print("  upload     Upload a local video/image to Google Flow")
    print("  sniff      Dev: capture Flow API requests for endpoint discovery")
    print("  credits    Show remaining Google Flow credits")
    print("  status     Check whether the backend is running")
    print("\nRun `flow <command> --help` for command-specific options.")


def main():
    # No arguments -> start the backend (most common use).
    if len(sys.argv) < 2:
        cmd_serve([])
        return

    if sys.argv[1] in ("-h", "--help", "help"):
        _usage()
        return

    command = sys.argv[1]
    handler = COMMANDS.get(command)
    if not handler:
        print(f"Unknown command: {command}\n", file=sys.stderr)
        _usage()
        sys.exit(2)

    handler(sys.argv[2:])


if __name__ == "__main__":
    main()
