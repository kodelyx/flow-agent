#!/usr/bin/env python3
"""System routes for Flow Agent.

Extension transport (WebSocket / HTTP callback), model listing, credits,
token refresh, and health/liveness endpoints.
"""

import time
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Header, WebSocket

from flow_server import state
from flow_server.state import verify_api_key, get_active_bridge

# Setup logging (format configured centrally in flow_engine/__init__.py, imported above)
log = logging.getLogger("flow_engine.openai_api")

router = APIRouter()


# Extension WebSocket and Callback Endpoints
@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log.info("Extension connecting via FastAPI WebSocket...")
    bridge = state.get_bridge()
    if bridge:
        await bridge.handle_fastapi_ws(websocket)
    else:
        log.error("Bridge not initialized")
        await websocket.close()

@router.post("/api/ext/callback")
async def http_callback(body: dict):
    bridge = state.get_bridge()
    if bridge:
        success = bridge.handle_http_callback(body)
        return {"ok": success}
    return {"ok": False}


# OpenAI Endpoints

@router.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    """List available Google Flow models (OpenAI format)."""
    return {
        "object": "list",
        "data": [
            {"id": "harbor_seal", "object": "model", "created": int(time.time()), "owned_by": "google"},
            {"id": "narwhal", "object": "model", "created": int(time.time()), "owned_by": "google"},
            {"id": "gem_pix_2", "object": "model", "created": int(time.time()), "owned_by": "google"}
        ]
    }


@router.get("/v1/credits", dependencies=[Depends(verify_api_key)])
async def get_flow_credits(x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    """Credits for connected Chrome clients.

    - With an X-Client-Id header: returns just that client's credits.
    - Without it: queries every connected client and returns per-client
      breakdown plus the cumulative pool across all browsers.
    """
    active_bridge = await get_active_bridge()

    # Specific client requested → return only that one.
    if x_client_id:
        try:
            res = await active_bridge.api_request(
                "/v1/credits", body=None, captcha_action=None, method="GET",
                client_id=x_client_id,
            )
            return res
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Otherwise fan out across all connected clients.
    client_ids = list(active_bridge._clients.keys())
    if not client_ids:
        raise HTTPException(status_code=503, detail="No extensions connected")

    async def _one(cid):
        try:
            res = await active_bridge.api_request(
                "/v1/credits", body=None, captcha_action=None, method="GET",
                client_id=cid,
            )
            data = res.get("data", {}) if isinstance(res, dict) else {}
            return {
                "client_id": cid,
                "credits": int(data.get("credits", 0)),
                "tier": data.get("userPaygateTier"),
                "sku": data.get("sku"),
                "ok": res.get("status") == 200,
            }
        except Exception as e:
            return {"client_id": cid, "credits": 0, "ok": False, "error": str(e)}

    clients = await asyncio.gather(*[_one(cid) for cid in client_ids])
    total = sum(c["credits"] for c in clients if c.get("ok"))
    return {
        "total_credits": total,
        "client_count": len(clients),
        "clients": clients,
    }


@router.post("/v1/refresh-tokens", dependencies=[Depends(verify_api_key)])
async def refresh_tokens(x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    """Ask token-less clients to open/refresh their Flow tab so a token gets
    captured. With X-Client-Id only that client is nudged; without it, every
    connected client that currently has no token is nudged."""
    active_bridge = await get_active_bridge()

    if x_client_id:
        targets = [x_client_id]
    else:
        targets = [cid for cid in active_bridge._clients
                   if not active_bridge._tokens.get(cid)]

    if not targets:
        return {"nudged": 0, "clients": [], "detail": "All connected clients already have tokens"}

    async def _nudge(cid):
        try:
            await active_bridge.send_message_to(cid, {"method": "open_flow_tab"})
            await asyncio.sleep(6)
            await active_bridge.send_message_to(cid, {"method": "refresh_flow_tab"})
            return {"client_id": cid, "ok": True}
        except Exception as e:
            return {"client_id": cid, "ok": False, "error": str(e)}

    results = await asyncio.gather(*[_nudge(cid) for cid in targets])
    return {"nudged": len(results), "clients": results}


@router.get("/")
async def root():
    return {"status": "running", "service": "Flow Agent API"}


# Health Check
@router.get("/health")
async def health():
    bridge = state.get_bridge()
    if not bridge:
        return {"status": "starting", "connected": False}
    return {
        "status": "healthy" if await bridge.health_check() else "unauthorized_or_disconnected",
        "extension_connected": bridge._ws is not None,
        "has_flow_key": bridge._flow_key is not None
    }
