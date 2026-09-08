"""Draft-at-360p generation: --draft / draft:true request the _360p model key."""

import io

import main
from flow_server.routes.generation import _video_model_key


def test_draft_appends_360p_model_key():
    assert _video_model_key(4, True) == "abra_t2v_4s_360p"
    assert _video_model_key(6, False) == "abra_t2v_6s"
    assert _video_model_key(10, True, "custom_model") == "custom_model"


READY = {"status": "healthy", "extension_connected": True, "has_flow_key": True}


class DownloadResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def test_cli_draft_flag_sends_payload(monkeypatch, tmp_path):
    requested = tmp_path / "draft.mp4"
    captured = {}
    media_bytes = bytes([0, 0, 0, 24]) + b"ftypisomvideo"
    monkeypatch.setattr(main, "_wait_for_generation_ready", lambda: READY)

    def fake_post(path, payload, key, timeout):
        captured.update(path=path, payload=payload, key=key)
        return {"job_id": "job-draft", "status": "processing", "data": []}

    def fake_request(path, **kwargs):
        return {
            "job_id": "job-draft",
            "status": "succeeded",
            "data": [{"url": "https://media.invalid/video", "media_id": "video-1"}],
        }, 200

    monkeypatch.setattr(main, "_post_generation", fake_post)
    monkeypatch.setattr(main, "_request_json", fake_request)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: DownloadResponse(media_bytes),
    )

    main.cmd_video(["draft it", "--draft", "--output", str(requested)])

    assert captured["payload"]["draft"] is True
    assert requested.read_bytes() == media_bytes
