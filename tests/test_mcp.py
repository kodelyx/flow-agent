"""Tests for the MCP protocol layer.

`handle_message` is pure — a line in, the line to write out, or None for a
notification — so the whole protocol is testable without a transport, which is
the point of separating it that way.
"""

import json

import pytest

from flow_agent import mcp_server
from flow_agent.engine import EngineError


def ask(method, request_id=1, params=None):
    """One JSON-RPC request, as a client would write it."""
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


async def reply(method, request_id=1, params=None):
    raw = await mcp_server.handle_message(ask(method, request_id, params))
    assert raw is not None
    return json.loads(raw)


# --------------------------------------------------------------------------- #
# handshake
# --------------------------------------------------------------------------- #


async def test_initialize_echoes_the_clients_protocol_version():
    """Answering with a different version is how a client decides it cannot
    proceed, so the client's own version wins."""
    body = await reply("initialize", params={"protocolVersion": "2025-06-18"})
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["serverInfo"]["name"] == "flow-agent"
    assert "tools" in body["result"]["capabilities"]


async def test_initialize_falls_back_to_our_version():
    body = await reply("initialize", params={})
    assert body["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


async def test_ping_answers_with_an_empty_result():
    body = await reply("ping")
    assert body["result"] == {}


# --------------------------------------------------------------------------- #
# tool discovery
# --------------------------------------------------------------------------- #


async def test_tools_list_returns_every_tool_with_a_schema():
    body = await reply("tools/list")
    tools = body["result"]["tools"]

    assert [t["name"] for t in tools] == [
        "flow_generate_image",
        "flow_generate_video",
        "flow_get_balance",
        "flow_get_stats",
        "flow_upload_video",
    ]
    for tool in tools:
        assert tool["description"].strip()
        assert tool["inputSchema"]["type"] == "object"


async def test_every_required_property_is_declared():
    """A `required` name that is not in `properties` is a schema a client
    cannot satisfy — the model has no way to know what to send."""
    for tool in mcp_server.TOOLS:
        schema = tool["inputSchema"]
        declared = set(schema.get("properties", {}))
        for name in schema.get("required", []):
            assert name in declared, f"{tool['name']} requires undeclared {name}"


async def test_tool_enum_defaults_are_in_their_own_enum():
    """A default outside its enum is refused by a strict client."""
    for tool in mcp_server.TOOLS:
        for name, prop in tool["inputSchema"].get("properties", {}).items():
            if "enum" in prop and "default" in prop:
                assert prop["default"] in prop["enum"], f"{tool['name']}.{name}"


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


class FakeEngine:
    def __init__(self, result=None, raises=None):
        self.result = result if result is not None else {"status": "succeeded"}
        self.raises = raises
        self.calls = []

    async def generate_image(self, **kwargs):
        self.calls.append(("image", kwargs))
        if self.raises:
            raise self.raises
        return self.result

    async def generate_video(self, **kwargs):
        self.calls.append(("video", kwargs))
        if self.raises:
            raise self.raises
        return self.result

    async def get_balance(self, refresh=False):
        self.calls.append(("balance", {"refresh": refresh}))
        return self.result

    async def get_stats(self):
        self.calls.append(("stats", {}))
        return self.result

    async def upload_video(self, file_path):
        self.calls.append(("upload", {"file_path": file_path}))
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture()
def fake_engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(mcp_server, "engine", fake)
    return fake


async def test_tools_call_generates_an_image(fake_engine):
    body = await reply(
        "tools/call",
        params={
            "name": "flow_generate_image",
            "arguments": {"prompt": "a boat", "aspect": "16:9", "count": 2},
        },
    )
    assert "isError" not in body["result"]
    assert fake_engine.calls == [
        ("image", {"prompt": "a boat", "aspect": "16:9", "count": 2,
                   "all_accounts": False})
    ]


async def test_a_tool_result_is_one_text_block_of_json(fake_engine):
    fake_engine.result = {"job_id": "j1", "status": "succeeded"}
    body = await reply(
        "tools/call",
        params={"name": "flow_get_stats", "arguments": {}},
    )
    blocks = body["result"]["content"]
    assert len(blocks) == 1 and blocks[0]["type"] == "text"
    assert json.loads(blocks[0]["text"]) == {"job_id": "j1", "status": "succeeded"}


async def test_tools_call_defaults_are_applied(fake_engine):
    await reply(
        "tools/call",
        params={"name": "flow_generate_video", "arguments": {"prompt": "x"}},
    )
    _, kwargs = fake_engine.calls[-1]
    assert kwargs["aspect"] == "16:9"
    assert kwargs["duration"] == "8s"
    assert kwargs["quality"] == "720p"


async def test_a_missing_argument_is_an_error_result_not_a_crash(fake_engine):
    body = await reply(
        "tools/call", params={"name": "flow_generate_image", "arguments": {}}
    )
    result = body["result"]
    assert result["isError"] is True
    assert "prompt" in result["content"][0]["text"]
    assert fake_engine.calls == []


async def test_an_engine_failure_is_marked_as_an_error(fake_engine):
    """Marked, so the model does not read a failure as a result."""
    fake_engine.raises = EngineError(["/bin/flow", "image"], 1, "", "no session")
    body = await reply(
        "tools/call",
        params={"name": "flow_generate_image", "arguments": {"prompt": "x"}},
    )
    result = body["result"]
    assert result["isError"] is True
    assert "no session" in result["content"][0]["text"]


async def test_an_unknown_tool_is_an_error_result(fake_engine):
    body = await reply(
        "tools/call", params={"name": "flow_make_me_a_sandwich", "arguments": {}}
    )
    assert body["result"]["isError"] is True
    assert fake_engine.calls == []


async def test_tools_call_without_a_name_is_an_invalid_params_error(fake_engine):
    body = await reply("tools/call", params={"arguments": {}})
    assert body["error"]["code"] == -32602


async def test_upload_passes_the_path_through(fake_engine):
    await reply(
        "tools/call",
        params={"name": "flow_upload_video", "arguments": {"file_path": "/tmp/a.mp4"}},
    )
    assert fake_engine.calls[-1] == ("upload", {"file_path": "/tmp/a.mp4"})


# --------------------------------------------------------------------------- #
# protocol rules
# --------------------------------------------------------------------------- #


async def test_a_notification_gets_no_reply():
    """MCP says a notification is unanswered. Writing one anyway desynchronises
    a client that is counting responses."""
    raw = json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    )
    assert await mcp_server.handle_message(raw) is None


async def test_a_request_with_no_id_is_treated_as_a_notification():
    raw = json.dumps({"jsonrpc": "2.0", "method": "tools/list"})
    assert await mcp_server.handle_message(raw) is None


async def test_a_malformed_line_is_a_parse_error():
    body = json.loads(await mcp_server.handle_message("{not json"))
    assert body["error"]["code"] == -32700
    assert body["id"] is None


async def test_a_non_object_message_is_an_invalid_request():
    body = json.loads(await mcp_server.handle_message("[1, 2, 3]"))
    assert body["error"]["code"] == -32600


async def test_an_unknown_method_is_method_not_found():
    body = await reply("resources/list")
    assert body["error"]["code"] == -32601


async def test_every_reply_carries_the_requests_own_id():
    """The id is how a client matches a response to its request; echoing the
    wrong one hangs the caller."""
    for request_id in (7, "abc", 0):
        body = await reply("ping", request_id=request_id)
        assert body["id"] == request_id
        assert body["jsonrpc"] == "2.0"
