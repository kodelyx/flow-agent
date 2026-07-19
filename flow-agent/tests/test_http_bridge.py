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


def test_duplicate_command_id_not_double_queued():
    reg = ExtensionHttpRegistry(session_ttl_sec=15)
    reg.hello(session_id="s1", flow_key="tok", secret="sec")
    cmd = {"id": "r1", "method": "get_status", "params": {}}
    assert reg.enqueue("s1", cmd) is True
    assert reg.enqueue("s1", cmd) is True
    first = reg.poll("s1")
    second = reg.poll("s1")
    assert len(first["commands"]) == 1
    assert first["commands"][0]["id"] == "r1"
    assert second["commands"] == []


def test_same_command_id_can_requeue_after_poll():
    reg = ExtensionHttpRegistry(session_ttl_sec=15)
    reg.hello(session_id="s1", flow_key="tok", secret="sec")
    cmd = {"id": "r1", "method": "get_status", "params": {}}
    assert reg.enqueue("s1", cmd) is True
    first = reg.poll("s1")
    assert len(first["commands"]) == 1
    assert first["commands"][0]["id"] == "r1"
    assert reg.enqueue("s1", cmd) is True
    second = reg.poll("s1")
    assert len(second["commands"]) == 1
    assert second["commands"][0]["id"] == "r1"


def test_enqueue_none_goes_to_latest_online():
    reg = ExtensionHttpRegistry(session_ttl_sec=15)
    reg.hello(session_id="s1", flow_key="tok1", secret="sec1")
    time.sleep(0.01)
    reg.hello(session_id="s2", flow_key="tok2", secret="sec2")
    assert reg.enqueue(None, {"id": "r1", "method": "get_status", "params": {}}) is True
    s1 = reg.poll("s1")
    s2 = reg.poll("s2")
    assert s1["commands"] == []
    assert len(s2["commands"]) == 1
    assert s2["commands"][0]["id"] == "r1"
