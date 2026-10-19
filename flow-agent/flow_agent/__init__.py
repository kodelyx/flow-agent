"""flow_agent package: Python interface, FastAPI server, and MCP server for Google Flow."""

from .characters import (
    CharacterRecord,
    CharacterRepository,
    PromptSegmentParser,
    R2VPayloadBuilder,
)
from .engine import EngineError, FlowEngine, extract_json
from .api import app
from .mcp_server import create_sse_app, run_stdio

__version__ = "1.0.0"

__all__ = [
    "FlowEngine",
    "EngineError",
    "extract_json",
    "CharacterRecord",
    "CharacterRepository",
    "PromptSegmentParser",
    "R2VPayloadBuilder",
    "app",
    "create_sse_app",
    "run_stdio",
    "__version__",
]
