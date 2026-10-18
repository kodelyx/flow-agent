"""flow_agent package: Python interface, FastAPI server, and MCP server for Google Flow."""

from .engine import EngineError, FlowEngine, extract_json
from .api import app
from .mcp_server import create_sse_app, run_stdio
from .auth import (
    create_api_key,
    delete_api_key,
    is_auth_enforced,
    list_api_keys,
    mask_key,
    revoke_api_key,
    verify_api_key,
)

__version__ = "1.0.0"

__all__ = [
    "FlowEngine",
    "EngineError",
    "extract_json",
    "app",
    "create_sse_app",
    "run_stdio",
    "create_api_key",
    "delete_api_key",
    "is_auth_enforced",
    "list_api_keys",
    "mask_key",
    "revoke_api_key",
    "verify_api_key",
    "__version__",
]
