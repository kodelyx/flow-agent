"""One entrypoint for the server, the MCP transports and a quick CLI.

The subcommands exist so the three ways of using this do not need three
different invocations to remember. Everything below is a thin wrapper: the work
is in the `flow_agent` package (`engine.py`, `api.py` and `mcp_server.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from flow_agent import EngineError, FlowEngine

engine = FlowEngine()


def _print(value) -> None:
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, default=str))


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #


def cmd_server(args) -> int:
    import uvicorn

    from flow_agent import app

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_mcp(args) -> int:
    from flow_agent import create_sse_app, run_stdio

    if not args.sse:
        asyncio.run(run_stdio())
        return 0

    import uvicorn

    uvicorn.run(create_sse_app(), host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_image(args) -> int:
    _print(
        asyncio.run(
            engine.generate_image(
                prompt=args.prompt,
                aspect=args.aspect,
                count=args.count,
                model=args.model,
                all_accounts=args.all,
                cookies=args.cookies,
            )
        )
    )
    return 0


def cmd_video(args) -> int:
    _print(
        asyncio.run(
            engine.generate_video(
                prompt=args.prompt,
                aspect=args.aspect,
                duration=args.duration,
                quality=args.quality,
                count=args.count,
                start_image=args.start_image,
                all_accounts=args.all,
                cookies=args.cookies,
            )
        )
    )
    return 0


def cmd_balance(args) -> int:
    _print(asyncio.run(engine.get_balance(refresh=args.refresh)))
    return 0


def cmd_stats(args) -> int:
    _print(asyncio.run(engine.get_stats()))
    return 0


def cmd_projects(args) -> int:
    _print(asyncio.run(engine.get_projects(cookies=args.cookies)))
    return 0


def cmd_key(args) -> int:
    """Create, list, revoke or delete the API keys this server accepts."""
    from flow_agent import create_api_key, delete_api_key, list_api_keys, mask_key, revoke_api_key
    from flow_agent.auth import KEY_PREFIX, KeyStoreError

    def label(target: str) -> str:
        # Echo a masked form of a key string; never print the secret itself.
        return mask_key(target) if target.startswith(KEY_PREFIX) else target

    action = args.key_action
    try:
        if action == "create":
            entry = create_api_key(name=args.name)
            key_value = entry["key"]
            print("=" * 68)
            print("  New Google Flow API key created.")
            print("=" * 68)
            print(f"  - Name    : {entry['name']}")
            print(f"  - API Key : {key_value}")
            print(f"  - Created : {entry['created_at']}")
            print("=" * 68)
            print("  [!] Store this key somewhere safe; it is not shown again.")
            print("  OpenAI SDK example:")
            print(f'    client = OpenAI(base_url="http://<host>:8001/v1", api_key="{key_value}")')
            print("=" * 68)
            return 0

        if action == "list":
            keys = list_api_keys()
            if not keys:
                print("No API keys registered. Create one with 'python main.py key create'.")
                return 0
            print(f"{'State':<8} | {'Name':<20} | {'API Key':<24} | Created")
            print("-" * 75)
            for k in keys:
                state = "active" if k.get("active", True) else "revoked"
                print(
                    f"{state:<8} | {k.get('name', 'default'):<20} | "
                    f"{mask_key(k.get('key', '')):<24} | {k.get('created_at', '')}"
                )
            return 0

        if action == "revoke":
            if revoke_api_key(args.target):
                print(f"Revoked API key '{label(args.target)}'.")
                return 0
            print(f"error: no API key or alias named '{label(args.target)}'", file=sys.stderr)
            return 1

        if action == "delete":
            if delete_api_key(args.target):
                print(f"Deleted API key '{label(args.target)}'.")
                return 0
            print(f"error: no API key or alias named '{label(args.target)}'", file=sys.stderr)
            return 1
    except (KeyStoreError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flow-agent",
        description="FastAPI + MCP server for Google Flow image and video generation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    server = sub.add_parser("server", help="run the FastAPI server")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8001)
    server.add_argument("--log-level", default="info")
    server.set_defaults(func=cmd_server)

    mcp = sub.add_parser("mcp", help="run the MCP server (stdio by default)")
    mcp.add_argument(
        "--sse",
        action="store_true",
        help="serve MCP over SSE instead of stdio, for a web client",
    )
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8002)
    mcp.add_argument("--log-level", default="info")
    mcp.set_defaults(func=cmd_mcp)

    image = sub.add_parser("image", help="generate an image")
    image.add_argument("prompt")
    image.add_argument(
        "--aspect",
        default="1:1",
        choices=["1:1", "16:9", "9:16", "4:3", "3:4", "square", "landscape", "portrait"],
        help="image aspect ratio",
    )
    image.add_argument(
        "--count",
        type=int,
        default=1,
        choices=[1, 2, 3, 4],
        help="number of images (1-4)",
    )
    image.add_argument("--model", default="narwhal")
    image.add_argument("--all", action="store_true", help="run on every account at once")
    image.add_argument("--cookies", default=None)
    image.set_defaults(func=cmd_image)

    video = sub.add_parser("video", help="generate a video")
    video.add_argument("prompt")
    video.add_argument(
        "--aspect",
        default="landscape",
        choices=["landscape", "portrait", "16:9", "9:16"],
        help="video aspect ratio",
    )
    video.add_argument(
        "--duration",
        default="8s",
        choices=["4s", "6s", "8s", "10s"],
        help="video duration",
    )
    video.add_argument(
        "--quality",
        default="720p",
        choices=["360p", "720p"],
        help="render quality",
    )
    video.add_argument(
        "--count",
        type=int,
        default=1,
        choices=[1, 2, 3, 4],
        help="number of videos (1-4)",
    )
    video.add_argument("--start-image", default=None, help="local image path or media ID")
    video.add_argument("--all", action="store_true")
    video.add_argument("--cookies", default=None)
    video.set_defaults(func=cmd_video)

    balance = sub.add_parser("balance", help="show credits per account")
    balance.add_argument(
        "--refresh",
        action="store_true",
        help="probe each account upstream before reporting",
    )
    balance.set_defaults(func=cmd_balance)

    stats = sub.add_parser("stats", help="show generation history")
    stats.set_defaults(func=cmd_stats)

    projects = sub.add_parser("projects", help="list the account's Flow projects")
    projects.add_argument("--cookies", default=None)
    projects.set_defaults(func=cmd_projects)

    key_parser = sub.add_parser("key", help="manage API keys for server authentication")
    key_sub = key_parser.add_subparsers(dest="key_action", required=True)

    key_create = key_sub.add_parser("create", help="generate a new API key")
    key_create.add_argument("--name", default="default", help="name or alias for this key")
    key_create.set_defaults(func=cmd_key)

    key_list = key_sub.add_parser("list", help="list all registered API keys")
    key_list.set_defaults(func=cmd_key)

    key_revoke = key_sub.add_parser("revoke", help="deactivate an API key")
    key_revoke.add_argument("target", help="API key string or name to deactivate")
    key_revoke.set_defaults(func=cmd_key)

    key_delete = key_sub.add_parser("delete", help="permanently delete an API key")
    key_delete.add_argument("target", help="API key string or name to delete")
    key_delete.set_defaults(func=cmd_key)

    return parser



def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except EngineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
