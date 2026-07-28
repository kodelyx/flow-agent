#!/usr/bin/env python3
"""MCP SSE transport for Flow Agent.

Exposes the MCP tool surface over SSE (GET /sse) plus the JSON-RPC message
sink (POST /messages) that the SSE endpoint event points clients at.
"""

import uuid
import json
import logging
import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from flow_server.config import MCP_SERVER_VERSION
from flow_server.mcp.tools import get_mcp_tools_list
from flow_server.mcp.executor import execute_mcp_tool

log = logging.getLogger("flow_engine.openai_api")

router = APIRouter()

SSE_CLIENTS = {}

@router.get("/sse")
async def sse_endpoint(request: Request):
    session_id = str(uuid.uuid4())
    queue = asyncio.Queue()
    SSE_CLIENTS[session_id] = queue
    log.info(f"[MCP SSE] Client session created: {session_id}")

    # Get request host and scheme, taking proxy headers into account
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    base_url = f"{scheme}://{host}"
    endpoint_url = f"{base_url}/messages?session_id={session_id}"
    log.info(f"[MCP SSE] Exposing endpoint URL: {endpoint_url}")

    async def event_generator():
        try:
            # According to the MCP SSE spec, we must send an "endpoint" event with the message URI
            yield f"event: endpoint\ndata: {endpoint_url}\n\n"

            while True:
                if await request.is_disconnected():
                    log.info(f"[MCP SSE] Client disconnected: {session_id}")
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Keeps the connection warm through proxies and lets the
                    # disconnect check above run at least once per second.
                    yield ": keepalive\n\n"
                    continue
                yield f"event: message\ndata: {json.dumps(message)}\n\n"
        finally:
            if session_id in SSE_CLIENTS:
                del SSE_CLIENTS[session_id]
                log.info(f"[MCP SSE] Client session cleaned up: {session_id}")

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/messages")
async def messages_endpoint(request: Request, session_id: str = Query(...)):
    if session_id not in SSE_CLIENTS:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    try:
        body_bytes = await request.body()
        if not body_bytes:
            raise HTTPException(status_code=400, detail="Empty request body")
        req_data = json.loads(body_bytes.decode("utf-8"))
        log.info(f"[MCP SSE] Received request: {req_data}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    method = req_data.get("method")
    request_id = req_data.get("id")

    response = None
    if method == "initialize":
        client_ver = (req_data.get("params") or {}).get("protocolVersion") or "2024-11-05"
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": client_ver,
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "flow-mcp",
                    "version": MCP_SERVER_VERSION
                }
            }
        }
    elif method in ("initialized", "notifications/initialized"):
        # MCP initialization notification — nothing to answer.
        return JSONResponse({"ok": True})
    elif method == "tools/list":
        tools = get_mcp_tools_list()
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": tools
            }
        }
    elif method == "tools/call":
        params = req_data.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        response = await execute_mcp_tool(request_id, tool_name, arguments)
    elif method == "ping":
        response = {"jsonrpc": "2.0", "id": request_id, "result": {}}
    elif request_id is not None:
        # An unknown *request* (not a notification) must still be answered —
        # otherwise the client blocks on the SSE stream waiting for an id
        # that never arrives.
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}"
            }
        }

    if response:
        await SSE_CLIENTS[session_id].put(response)

    return JSONResponse({"ok": True})
