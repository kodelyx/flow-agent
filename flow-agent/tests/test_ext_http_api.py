from fastapi.testclient import TestClient

import cli.api as api


def test_hello_and_poll_roundtrip():
    with TestClient(api.app) as client:
        r = client.post("/api/ext/hello", json={
            "session_id": "s-test",
            "flowKeyPresent": True,
            "flowKey": "tok-1",
            "extension_version": "1.0.0",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        secret = body["secret"]
        assert secret
        assert body["callback_url"].endswith("/api/ext/callback")
        assert body["poll_url"].endswith("/api/ext/poll")
        assert api.bridge is not None
        api.bridge.enqueue_http_command({"id": "c1", "method": "get_status", "params": {}})
        p = client.get(
            "/api/ext/poll",
            params={"session_id": "s-test"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert p.status_code == 200
        assert p.json()["commands"][0]["id"] == "c1"


def test_poll_requires_authorization():
    with TestClient(api.app) as client:
        r = client.post("/api/ext/hello", json={
            "session_id": "s-auth",
            "flowKey": "tok",
        })
        assert r.status_code == 200
        p = client.get("/api/ext/poll", params={"session_id": "s-auth"})
        assert p.status_code == 401


def test_health_reports_http_transport():
    with TestClient(api.app) as client:
        r = client.post("/api/ext/hello", json={
            "session_id": "s-health",
            "flowKeyPresent": True,
            "flowKey": "tok-health",
        })
        assert r.status_code == 200
        h = client.get("/health")
        assert h.status_code == 200
        body = h.json()
        assert body["extension_connected"] is True
        assert body["has_flow_key"] is True
        assert body["transport"] == "http"
