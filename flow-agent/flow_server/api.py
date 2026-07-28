#!/usr/bin/env python3
"""OpenAI-Compatible API Server for Flow Agent.

Implements standard OpenAI API specs (e.g. /v1/images/generations)
for seamless integration with OpenAI clients and external tools (n8n, Dify, etc.).
"""

# This module is now a thin back-compat shim. The implementation lives in:
#   flow_server/config.py   constants and pure helpers
#   flow_server/models.py   pydantic request models
#   flow_server/state.py    bridge singleton, lifespan, auth, history helpers
#   flow_server/routes/     HTTP endpoints
#   flow_server/mcp/        MCP tool surface + SSE transport
#   flow_server/app.py      FastAPI app assembly
# 'flow_server.api:app' remains the uvicorn target, and the names re-exported below stay
# importable from here for existing callers.

import os
import sys

# Add parent dir to sys.path so flow_engine can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flow_server.app import app
from flow_server.config import (
    INPUT_DIR,
    OUTPUT_DIR,
    public_url,
)
from flow_server.models import (
    ChatCompletionRequest,
    ChatMessage,
    ImageGenerationRequest,
    UploadRequest,
    VideoGenerationRequest,
)
from flow_server.state import (
    append_to_history,
    get_active_bridge,
    verify_api_key,
)
from flow_server.routes.generation import openai_generate_image, openai_generate_video
from flow_server.routes.media import get_history, upload_file_endpoint
from flow_server.routes.system import get_flow_credits, health, list_models
from flow_server.mcp.executor import execute_mcp_tool
from flow_server.mcp.sse import SSE_CLIENTS
from flow_server.mcp.tools import get_mcp_tools_list

__all__ = [
    "app",
    "openai_generate_image",
    "openai_generate_video",
    "get_flow_credits",
    "get_history",
    "health",
    "list_models",
    "upload_file_endpoint",
    "execute_mcp_tool",
    "get_mcp_tools_list",
    "SSE_CLIENTS",
    "OUTPUT_DIR",
    "INPUT_DIR",
    "public_url",
    "append_to_history",
    "get_active_bridge",
    "verify_api_key",
    "ImageGenerationRequest",
    "VideoGenerationRequest",
    "ChatMessage",
    "ChatCompletionRequest",
    "UploadRequest",
]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Flow Agent OpenAI API Server")
    parser.add_argument("--host", default=os.environ.get("OPENAI_API_HOST", "127.0.0.1"), help="Host address")
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPENAI_API_PORT", "8001")), help="Port to run on")
    args = parser.parse_args()

    import uvicorn
    uvicorn.run("flow_server.api:app", host=args.host, port=args.port,
                timeout_graceful_shutdown=int(os.environ.get("SHUTDOWN_TIMEOUT", "5")))
