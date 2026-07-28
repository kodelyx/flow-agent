import io
import subprocess

import pytest
from PIL import Image

import main
from flow_server.media_types import sniff_media_type


READY = {
    "status": "healthy",
    "extension_connected": True,
    "has_flow_key": True,
}


class DownloadResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def _image_bytes(image_format, size=(47, 31)):
    encoded = io.BytesIO()
    Image.new("RGB", size, (24, 96, 180)).save(encoded, format=image_format)
    return encoded.getvalue()


def test_existing_backend_is_reused_without_starting_another_process(monkeypatch):
    monkeypatch.setattr(main, "_probe_backend", lambda timeout=2: READY)

    def unexpected_popen(*args, **kwargs):
        raise AssertionError("a second backend must not be started")

    monkeypatch.setattr(subprocess, "Popen", unexpected_popen)

    assert main._ensure_backend() == READY


def test_run_backend_reuses_service_without_starting_uvicorn(monkeypatch, capsys):
    monkeypatch.setattr(main, "_probe_backend", lambda timeout=2: READY)

    main.run_backend([])

    assert "already running" in capsys.readouterr().out


def test_generation_waits_for_extension_and_flow_key(monkeypatch):
    states = iter(
        [
            {"status": "healthy", "extension_connected": True, "has_flow_key": False},
            READY,
        ]
    )
    monkeypatch.setattr(
        main,
        "_ensure_backend",
        lambda: {"status": "healthy", "extension_connected": False, "has_flow_key": False},
    )
    monkeypatch.setattr(main, "_probe_backend", lambda timeout=2: next(states))
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)

    assert main._wait_for_generation_ready(timeout=5) == READY


def test_generation_readiness_failure_is_actionable_and_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        main,
        "_ensure_backend",
        lambda: {"status": "healthy", "extension_connected": False, "has_flow_key": False},
    )

    with pytest.raises(SystemExit) as caught:
        main._wait_for_generation_ready(timeout=0)

    assert caught.value.code == main.EXIT_NOT_READY
    error = capsys.readouterr().err
    assert "extension_connected=False" in error
    assert "has_flow_key=False" in error
    assert "Open Google Flow in Chrome" in error


def test_image_output_uses_exact_requested_absolute_path(monkeypatch, tmp_path, capsys):
    requested = tmp_path / "nested" / "exact.custom-extension"
    monkeypatch.setattr(main, "_wait_for_generation_ready", lambda: READY)
    monkeypatch.setattr(
        main,
        "_post_generation",
        lambda *args, **kwargs: {
            "data": [{"url": "https://media.invalid/generated", "media_id": "image-1"}]
        },
    )
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: DownloadResponse(b"\x89PNG\r\n\x1a\ncontent"),
    )

    main.cmd_image(["a lighthouse", "--output", str(requested)])

    assert requested.read_bytes() == b"\x89PNG\r\n\x1a\ncontent"
    assert list(requested.parent.iterdir()) == [requested]
    output = capsys.readouterr().out
    assert str(requested) in output
    assert "format: PNG" in output


@pytest.mark.parametrize(
    ("suffix", "source_format", "expected_mime"),
    [
        (".png", "JPEG", "image/png"),
        (".jpg", "PNG", "image/jpeg"),
        (".jpeg", "PNG", "image/jpeg"),
        (".webp", "PNG", "image/webp"),
    ],
)
def test_explicit_image_output_is_converted_to_requested_signature_and_keeps_dimensions(
    monkeypatch, tmp_path, suffix, source_format, expected_mime
):
    requested = tmp_path / f"test{suffix}"
    source_size = (47, 31)
    source_bytes = _image_bytes(source_format, source_size)
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: DownloadResponse(source_bytes),
    )

    actual = main._download_media(
        "https://media.invalid/generated", str(requested), preserve_explicit=True
    )

    assert actual == str(requested)
    with requested.open("rb") as saved_file:
        assert sniff_media_type(saved_file) == expected_mime
    with Image.open(requested) as saved_image:
        assert saved_image.size == source_size
    assert not [path for path in tmp_path.iterdir() if ".flow-" in path.name]


def test_explicit_image_conversion_uses_atomic_replace(monkeypatch, tmp_path):
    from flow_server import media_types

    requested = tmp_path / "atomic.png"
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: DownloadResponse(_image_bytes("JPEG")),
    )
    actual_replace = media_types.os.replace
    replacements = []

    def tracked_replace(source, destination):
        replacements.append((source, destination))
        return actual_replace(source, destination)

    monkeypatch.setattr(media_types.os, "replace", tracked_replace)

    main._download_media(
        "https://media.invalid/generated", str(requested), preserve_explicit=True
    )

    assert replacements == [(replacements[0][0], str(requested))]
    assert ".flow-convert-" in replacements[0][0]


def test_explicit_image_conversion_failure_is_clear_and_nonzero(
    monkeypatch, tmp_path, capsys
):
    requested = tmp_path / "broken.png"
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: DownloadResponse(b"\xff\xd8\xffnot-a-valid-jpeg"),
    )

    with pytest.raises(SystemExit) as caught:
        main._download_media(
            "https://media.invalid/generated", str(requested), preserve_explicit=True
        )

    assert caught.value.code == main.EXIT_OUTPUT_ERROR
    assert "could not convert image to PNG" in capsys.readouterr().err
    assert not requested.exists()
    assert not list(tmp_path.iterdir())


def test_first_of_multiple_images_keeps_exact_output_path(monkeypatch, tmp_path):
    requested = tmp_path / "result.png"
    monkeypatch.setattr(main, "_wait_for_generation_ready", lambda: READY)
    monkeypatch.setattr(
        main,
        "_post_generation",
        lambda *args, **kwargs: {
            "data": [
                {"url": "https://media.invalid/one"},
                {"url": "https://media.invalid/two"},
            ]
        },
    )
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: DownloadResponse(b"\x89PNG\r\n\x1a\ncontent"),
    )

    main.cmd_image(["two images", "--count", "2", "--output", str(requested)])

    assert requested.exists()
    assert (tmp_path / "result_2.png").exists()


def test_auto_output_extension_comes_from_file_signature(monkeypatch, tmp_path):
    automatic_path = tmp_path / "generated.png"
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: DownloadResponse(b"\xff\xd8\xffjpeg-content"),
    )

    actual = main._download_media(
        "https://media.invalid/jpeg", str(automatic_path), preserve_explicit=False
    )

    assert actual == str(tmp_path / "generated.jpg")
    assert not automatic_path.exists()
    assert (tmp_path / "generated.jpg").read_bytes().startswith(b"\xff\xd8\xff")


def test_explicit_output_rejects_non_media_download(monkeypatch, tmp_path):
    requested = tmp_path / "exact.png"
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: DownloadResponse(b"<html>upstream error</html>"),
    )

    with pytest.raises(SystemExit) as caught:
        main._download_media(
            "https://media.invalid/broken", str(requested), preserve_explicit=True
        )

    assert caught.value.code == main.EXIT_OUTPUT_ERROR
    assert not requested.exists()


def test_image_to_video_uses_backend_job_polling_and_exact_output(monkeypatch, tmp_path):
    start_image = tmp_path / "misleading.jpg"
    start_image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    requested = tmp_path / "movie.bin"
    captured = {}

    monkeypatch.setattr(main, "_wait_for_generation_ready", lambda: READY)

    def fake_post(path, payload, key, timeout):
        captured.update(path=path, payload=payload, key=key)
        return {"job_id": "job-1", "status": "processing", "data": []}

    def fake_request(path, **kwargs):
        assert path == "/v1/videos/generations/job-1"
        return {
            "job_id": "job-1",
            "status": "succeeded",
            "data": [{"url": "https://media.invalid/video", "media_id": "video-1"}],
        }, 200

    monkeypatch.setattr(main, "_post_generation", fake_post)
    monkeypatch.setattr(main, "_request_json", fake_request)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: DownloadResponse(b"\x00\x00\x00\x18ftypisomvideo"),
    )

    main.cmd_video(
        ["animate it", "--start", str(start_image), "--output", str(requested)]
    )

    assert captured["path"] == "/v1/videos/generations"
    assert captured["payload"]["image_base64"].startswith("data:image/png;base64,")
    assert requested.read_bytes() == b"\x00\x00\x00\x18ftypisomvideo"


def test_first_last_frame_cli_passes_exact_history_media_ids(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "_wait_for_generation_ready", lambda: READY)

    def fake_post(path, payload, key, timeout):
        captured.update(path=path, payload=payload, key=key)
        return {
            "job_id": "job-first-last",
            "status": "succeeded",
            "data": [{"url": "https://media.invalid/video"}],
        }

    monkeypatch.setattr(main, "_post_generation", fake_post)
    monkeypatch.setattr(main, "_save_outputs", lambda *args, **kwargs: [])

    main.cmd_video([
        "transition",
        "--start",
        "generated-history-media-id",
        "--end",
        "uploaded-history-media-id",
    ])

    assert captured["path"] == "/v1/videos/generations"
    assert captured["payload"]["start_media_id"] == "generated-history-media-id"
    assert captured["payload"]["end_media_id"] == "uploaded-history-media-id"


def test_upload_uses_signature_mime_and_backend_not_local_bridge(monkeypatch, tmp_path):
    video = tmp_path / "wrong.png"
    video.write_bytes(b"\x00\x00\x00\x18ftypisomvideo")
    captured = {}
    monkeypatch.setattr(main, "_wait_for_generation_ready", lambda: READY)

    def fake_request(path, **kwargs):
        captured.update(path=path, **kwargs)
        return {"media_id": "uploaded-1", "url": "http://localhost/media"}, 200

    monkeypatch.setattr(main, "_request_json", fake_request)

    main.cmd_upload([str(video)])

    assert captured["path"] == "/v1/upload"
    assert captured["payload"]["image_base64"].startswith("data:video/mp4;base64,")


def test_generation_retry_reuses_same_idempotency_key(monkeypatch):
    calls = []

    def fake_request(path, **kwargs):
        calls.append(kwargs["headers"]["Idempotency-Key"])
        if len(calls) == 1:
            raise main.CliError("connection reset", main.EXIT_BACKEND_UNAVAILABLE, retryable=True)
        return {"data": [{"url": "https://media.invalid/image"}]}, 200

    monkeypatch.setattr(main, "_request_json", fake_request)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)

    result = main._post_generation(
        "/v1/images/generations", {"prompt": "safe retry"}, "same-key"
    )

    assert result["data"]
    assert calls == ["same-key", "same-key"]


def test_default_output_dir_honors_flow_output_dir(monkeypatch, tmp_path):
    destination = tmp_path / "custom-output"
    monkeypatch.setenv("FLOW_OUTPUT_DIR", str(destination))

    path, explicit = main._requested_output_path(None, "image.png")

    assert path == str(destination / "image.png")
    assert explicit is False
