"""MCP server over JSON-RPC 2.0, on stdio and on SSE.

The protocol layer is deliberately a pure function — `handle_message` takes a
line and returns the line to write, or None for a notification — so the tool
schemas and the dispatch can be tested without a transport, and so the two
transports cannot drift apart in what they answer.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any, Optional

from .engine import EngineError, FlowEngine

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "flow-agent", "version": "1.0.0"}

engine = FlowEngine()


# --------------------------------------------------------------------------- #
# tool schemas
# --------------------------------------------------------------------------- #

TOOLS: list[dict] = [
    {
        "name": "flow_generate_image",
        "description": (
            "Generate an image on Google Flow. Returns the job id, the account that "
            "served it and the path of each file written. Set all_accounts to run the "
            "same prompt on every configured account at once."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What to draw."},
                "aspect": {
                    "type": "string",
                    "description": "Aspect ratio. Accepts a ratio or a name.",
                    "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "square", "landscape", "portrait"],
                    "default": "1:1",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                    "default": 1,
                    "description": "Variations to ask for.",
                },
                "all_accounts": {
                    "type": "boolean",
                    "default": False,
                    "description": "Run on every account in cookies/ at once.",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "flow_generate_video",
        "description": (
            "Generate a video on Google Flow. The render is asynchronous: the media id "
            "comes back before the file exists unless the call waits for it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What to film."},
                "aspect": {
                    "type": "string",
                    "enum": ["16:9", "9:16", "landscape", "portrait"],
                    "default": "16:9",
                },
                "duration": {
                    "type": "string",
                    "enum": ["4s", "6s", "8s", "10s"],
                    "default": "8s",
                },
                "quality": {
                    "type": "string",
                    "enum": ["720p", "360p"],
                    "default": "720p",
                    "description": "720p costs more credits per render than 360p.",
                },
                "all_accounts": {"type": "boolean", "default": False},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "flow_get_balance",
        "description": (
            "Remaining generation credits for every configured account, with the total. "
            "Cached by default; refresh reads each account upstream first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Probe upstream before reporting, which is slower.",
                }
            },
        },
    },
    {
        "name": "flow_get_stats",
        "description": "Generation history and engine totals, read from the database.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "flow_upload_video",
        "description": (
            "Upload a local video into the account's project and return its media id, "
            "for use as a start image or as the source of an edit. Repeat uploads of "
            "the same file are cached on a content hash."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to a local video."}
            },
            "required": ["file_path"],
        },
    },
]


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def _text(value: Any) -> dict:
    """Wrap a result as the one content block MCP expects."""
    if not isinstance(value, str):
        value = json.dumps(value, indent=2, default=str)
    return {"content": [{"type": "text", "text": value}]}


async def call_tool(name: str, arguments: dict) -> dict:
    """Run one tool and return an MCP tool result."""
    try:
        if name == "flow_generate_image":
            result = await engine.generate_image(
                prompt=arguments["prompt"],
                aspect=arguments.get("aspect", "1:1"),
                count=int(arguments.get("count", 1) or 1),
                all_accounts=bool(arguments.get("all_accounts", False)),
            )
        elif name == "flow_generate_video":
            result = await engine.generate_video(
                prompt=arguments["prompt"],
                aspect=arguments.get("aspect", "16:9"),
                duration=arguments.get("duration", "8s"),
                quality=arguments.get("quality", "720p"),
                all_accounts=bool(arguments.get("all_accounts", False)),
            )
        elif name == "flow_get_balance":
            result = await engine.get_balance(refresh=bool(arguments.get("refresh", False)))
        elif name == "flow_get_stats":
            result = await engine.get_stats()
        elif name == "flow_upload_video":
            result = await engine.upload_video(arguments["file_path"])
        else:
            return {**_text(f"unknown tool {name!r}"), "isError": True}
    except KeyError as exc:
        return {**_text(f"missing required argument {exc}"), "isError": True}
    except EngineError as exc:
        # The engine's own words, and marked as an error so the model does not
        # read a failure as a result.
        return {**_text(str(exc)), "isError": True}

    return _text(result)


async def handle_message(raw: str) -> Optional[str]:
    """Answer one JSON-RPC message, or return None when none is due.

    None is the whole of the notification rule: MCP says a notification gets no
    reply, and writing one anyway desynchronises a client that is counting
    responses.
    """
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        return json.dumps(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {exc}"}}
        )

    if not isinstance(message, dict):
        return json.dumps(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "not a request"}}
        )

    method = message.get("method", "")
    request_id = message.get("id")

    if request_id is None:
        return None

    def ok(result: Any) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})

    def fail(code: int, text: str) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text}})

    if method == "initialize":
        # Echo the client's protocol version when it names one: MCP expects the
        # server to answer with the version it will speak, and answering with a
        # different one is how a client decides it cannot proceed.
        params = message.get("params") or {}
        return ok(
            {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }
        )

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if not name:
            return fail(-32602, "tools/call needs a name")
        return ok(await call_tool(name, params.get("arguments") or {}))

    if method.startswith("notifications/"):
        return None

    return fail(-32601, f"unknown method {method!r}")


# --------------------------------------------------------------------------- #
# stdio transport
# --------------------------------------------------------------------------- #


async def run_stdio() -> None:
    """Serve MCP over stdin/stdout, one JSON message per line.

    stdout carries the protocol and nothing else: a stray print would be read as
    a malformed message, so anything diagnostic belongs on stderr.
    """
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        reply = await handle_message(line)
        if reply is not None:
            sys.stdout.write(reply + "\n")
            sys.stdout.flush()


# --------------------------------------------------------------------------- #
# SSE transport
# --------------------------------------------------------------------------- #


def create_sse_app():
    """Build the SSE transport as a FastAPI app.

    The shape is MCP's: a client opens `GET /sse` and is handed the URL to POST
    to, then every request it sends there is answered on the stream it already
    has open. Responses are correlated by session id, because the stream is the
    only channel back.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(title="flow-agent MCP (SSE)")
    sessions: dict[str, asyncio.Queue] = {}

    @app.get("/sse")
    async def sse(request: Request):
        session_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        sessions[session_id] = queue

        async def events():
            # The endpoint event comes first and tells the client where to send.
            yield f"event: endpoint\ndata: /messages/?session_id={session_id}\n\n"
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield f"event: message\ndata: {item}\n\n"
            finally:
                sessions.pop(session_id, None)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/messages/")
    async def messages(request: Request):
        session_id = request.query_params.get("session_id", "")
        queue = sessions.get(session_id)
        if queue is None:
            return JSONResponse(
                {"error": "unknown session — open /sse first"}, status_code=404
            )

        raw = (await request.body()).decode("utf-8", "replace")
        reply = await handle_message(raw)
        if reply is not None:
            await queue.put(reply)
        # The answer travels on the stream, so the POST itself only acknowledges.
        return JSONResponse({"accepted": True}, status_code=202)

    return app
