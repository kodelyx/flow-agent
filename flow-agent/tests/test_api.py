"""Tests for the FastAPI surface, with the engine stubbed out.

No test here runs the binary. Every one of them replaces `api.engine`, because
the routes are what is under test — the engine has its own suite, and a route
test that spends a credit is not a test anyone will run.
"""

import json

import pytest
from fastapi.testclient import TestClient

from flow_agent import api
from flow_agent.engine import EngineError


class FakeEngine:
    """Records what it was asked for and returns a canned result."""

    #: `/health` reports whether the engine is reachable, so the stub has to
    #: answer the two attributes that route reads.
    binary = "/tmp/fake/flow"

    def __init__(self, image=None, video=None, balance=None, projects=None,
                 raises=None):
        self.image = image if image is not None else {
            "job_id": "j1",
            "account_id": "acct-1",
            "status": "succeeded",
            "files": [{"path": "/tmp/out/one.png"}],
        }
        self.video = video if video is not None else {
            "job_id": "j2",
            "media_ids": ["m1"],
            "status": "submitted",
        }
        self.balance = balance if balance is not None else {
            "accounts": [], "total_credits": 0, "unread": 0
        }
        self.projects = projects if projects is not None else []
        self.raises = raises
        self.calls = []

    async def generate_image(self, **kwargs):
        self.calls.append(("image", kwargs))
        if self.raises:
            raise self.raises
        return self.image

    async def generate_video(self, **kwargs):
        self.calls.append(("video", kwargs))
        if self.raises:
            raise self.raises
        return self.video

    async def get_balance(self, refresh=False):
        self.calls.append(("balance", {"refresh": refresh}))
        return self.balance

    async def get_stats(self):
        self.calls.append(("stats", {}))
        return {"generations": [], "totals": {}}

    async def get_projects(self, cookies=None):
        self.calls.append(("projects", {"cookies": cookies}))
        return self.projects

    def available(self):
        return True


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """A TestClient over the real routes with a stubbed engine."""
    fake = FakeEngine()
    monkeypatch.setattr(api, "engine", fake)
    # The media route resolves against this, so it must point somewhere the
    # test owns rather than at the real output directory.
    monkeypatch.setattr(api, "MEDIA_DIR", tmp_path)
    with TestClient(api.app) as c:
        c.fake = fake
        c.media_dir = tmp_path
        yield c


# --------------------------------------------------------------------------- #
# health and reads
# --------------------------------------------------------------------------- #


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"


def test_balance_route(client):
    client.fake.balance = {"accounts": [], "total_credits": 42, "unread": 0}
    assert client.get("/api/v1/balance").json()["total_credits"] == 42


def test_balance_refresh_is_passed_through(client):
    client.get("/api/v1/balance?refresh=true")
    assert ("balance", {"refresh": True}) in client.fake.calls


def test_stats_route(client):
    assert client.get("/api/v1/stats").status_code == 200


def test_projects_route_wraps_the_list(client):
    client.fake.projects = [{"id": "p1"}]
    assert client.get("/api/v1/projects").json() == {"projects": [{"id": "p1"}]}


# --------------------------------------------------------------------------- #
# OpenAI-compatible images
# --------------------------------------------------------------------------- #


def test_openai_images_maps_a_size_to_an_aspect(client):
    response = client.post(
        "/v1/images/generations",
        json={"prompt": "a paper boat", "size": "1024x1792"},
    )
    assert response.status_code == 200
    _, kwargs = client.fake.calls[-1]
    assert kwargs["aspect"] == "9:16"


def test_openai_images_returns_a_fetchable_url(client):
    body = client.post(
        "/v1/images/generations", json={"prompt": "a paper boat"}
    ).json()
    assert body["data"][0]["url"].endswith("/api/v1/media/one.png")
    # The engine's own result is carried alongside, so a caller that wants the
    # job id or the account does not have to guess it.
    assert body["flow"]["job_id"] == "j1"


def test_openai_images_refuses_an_unknown_size(client):
    response = client.post(
        "/v1/images/generations", json={"prompt": "x", "size": "640x480"}
    )
    assert response.status_code == 400
    assert "640x480" in response.json()["detail"]
    # Refused before the engine is reached, so nothing was spent.
    assert client.fake.calls == []


def test_openai_images_accepts_a_size_name(client):
    client.post("/v1/images/generations", json={"prompt": "x", "size": "portrait"})
    assert client.fake.calls[-1][1]["aspect"] == "9:16"


def test_openai_images_passes_the_count_through(client):
    client.post("/v1/images/generations", json={"prompt": "x", "n": 3})
    assert client.fake.calls[-1][1]["count"] == 3


def test_openai_images_ignores_fields_it_does_not_model(client):
    """An OpenAI client sends keys this does not know; failing on them would
    make the endpoint unusable from the SDKs it exists to serve."""
    response = client.post(
        "/v1/images/generations",
        json={"prompt": "x", "quality": "hd", "style": "vivid", "user": "u1"},
    )
    assert response.status_code == 200


def test_openai_images_is_not_a_200_when_nothing_was_produced(client):
    """A refusal is reported as a failure.

    The engine exits zero and reports the refusal in the result, so returning
    200 with an empty `data` array would tell a client the request worked and
    there simply were no images.
    """
    client.fake.image = {
        "job_id": "j1",
        "account_id": "acct-1",
        "status": "empty",
        "files": [],
    }
    response = client.post("/v1/images/generations", json={"prompt": "x"})
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "empty" in detail and "j1" in detail


def test_openai_images_reports_an_engine_error_as_502(client):
    client.fake.raises = EngineError(["/bin/flow", "image"], 1, "", "no session")
    response = client.post("/v1/images/generations", json={"prompt": "x"})
    assert response.status_code == 502
    assert "no session" in response.json()["detail"]


def test_openai_images_supports_b64_json(client):
    target = client.media_dir / "one.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    client.fake.image = {"status": "succeeded", "files": [{"path": str(target)}]}

    body = client.post(
        "/v1/images/generations",
        json={"prompt": "x", "response_format": "b64_json"},
    ).json()

    assert "b64_json" in body["data"][0]
    assert "url" not in body["data"][0]


def test_openai_images_reports_an_unreadable_file_as_a_failure(client):
    """A file that vanished between the run and the read is not a success."""
    client.fake.image = {
        "status": "succeeded",
        "files": [{"path": str(client.media_dir / "gone.png")}],
    }
    response = client.post(
        "/v1/images/generations",
        json={"prompt": "x", "response_format": "b64_json"},
    )
    assert response.status_code == 502


# --------------------------------------------------------------------------- #
# OpenAI-compatible chat
# --------------------------------------------------------------------------- #


def test_chat_uses_the_last_user_turn(client):
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "you draw things"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "a paper boat"},
            ]
        },
    )
    assert client.fake.calls[-1][1]["prompt"] == "a paper boat"


def test_chat_reads_a_list_of_content_parts(client):
    """The newer OpenAI shape sends parts, not a string."""
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "a paper"},
                        {"type": "image_url", "image_url": {"url": "http://x"}},
                        {"type": "text", "text": "boat"},
                    ],
                }
            ]
        },
    )
    assert client.fake.calls[-1][1]["prompt"] == "a paper boat"


def test_chat_refuses_when_there_is_no_user_text(client):
    response = client.post(
        "/v1/chat/completions", json={"messages": [{"role": "system", "content": "hi"}]}
    )
    assert response.status_code == 400
    assert client.fake.calls == []


def test_chat_answers_with_a_markdown_image(client):
    body = client.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}
    ).json()
    content = body["choices"][0]["message"]["content"]
    assert content.startswith("![generated image](")
    assert body["object"] == "chat.completion"


def test_chat_reports_a_production_failure_in_the_content(client):
    """The chat route cannot use a status code for this, so the failure has to
    be in the text — a bare "done" would read as a successful generation."""
    client.fake.image = {"status": "empty", "job_id": "j9", "files": []}
    body = client.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}
    ).json()
    content = body["choices"][0]["message"]["content"]
    assert "no image" in content and "j9" in content


def test_chat_streams_sse_frames_ending_in_done(client):
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "x"}], "stream": True},
    )
    assert response.headers["content-type"].startswith("text/event-stream")

    payloads = [
        line[len("data: "):]
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert payloads[-1] == "[DONE]"

    chunks = [json.loads(p) for p in payloads[:-1]]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)


# --------------------------------------------------------------------------- #
# native routes
# --------------------------------------------------------------------------- #


def test_native_image_route_speaks_aspect_ratios(client):
    client.post("/api/v1/image", json={"prompt": "x", "aspect": "3:4", "count": 2})
    _, kwargs = client.fake.calls[-1]
    assert kwargs["aspect"] == "3:4" and kwargs["count"] == 2


def test_native_video_route(client):
    client.post(
        "/api/v1/video",
        json={"prompt": "x", "aspect": "9:16", "duration": "6s", "quality": "360p"},
    )
    _, kwargs = client.fake.calls[-1]
    assert kwargs == {
        "prompt": "x",
        "aspect": "9:16",
        "duration": "6s",
        "quality": "360p",
        "count": 1,
        "start_image": None,
        "all_accounts": False,
        "cookies": None,
    }


def test_native_image_route_rejects_a_count_out_of_range(client):
    assert client.post("/api/v1/image", json={"prompt": "x", "count": 0}).status_code == 422


# --------------------------------------------------------------------------- #
# media serving
# --------------------------------------------------------------------------- #


def test_media_serves_a_real_file(client):
    (client.media_dir / "ok.png").write_bytes(b"png-bytes")
    response = client.get("/api/v1/media/ok.png")
    assert response.status_code == 200
    assert response.content == b"png-bytes"


def test_media_refuses_a_traversal(client):
    """The route takes a name out of a URL, and `..` in one is the whole attack."""
    secret = client.media_dir.parent / "secret.txt"
    secret.write_text("do not serve me")

    for name in ("../secret.txt", "..%2Fsecret.txt", "%2e%2e%2fsecret.txt"):
        assert client.get(f"/api/v1/media/{name}").status_code == 404


def test_media_refuses_a_missing_file(client):
    assert client.get("/api/v1/media/absent.png").status_code == 404
