# flow-agent/tests/test_http_bridge.py
import time
from omniflash.http_bridge import ExtensionHttpRegistry


def test_hello_registers_session_and_accepts_token():
    reg = ExtensionHttpRegistry(session_ttl_sec=15)
    out = reg.hello(session_id="s1", flow_key="tok", secret="test-secret")
    assert out["ok"] is True
    assert reg.is_connected("s1") is True
    assert reg.get_flow_key("s1") == "tok"


def test_enqueue_and_poll_returns_command_once():
    reg = ExtensionHttpRegistry(session_ttl_sec=15)
    reg.hello(session_id="s1", flow_key="tok", secret="sec")
    reg.enqueue("s1", {"id": "r1", "method": "get_status", "params": {}})
    first = reg.poll("s1")
    second = reg.poll("s1")
    assert len(first["commands"]) == 1
    assert first["commands"][0]["id"] == "r1"
    assert second["commands"] == []


def test_session_expires():
    reg = ExtensionHttpRegistry(session_ttl_sec=1)
    reg.hello(session_id="s1", flow_key="tok", secret="sec")
    time.sleep(1.2)
    assert reg.is_connected("s1") is False
