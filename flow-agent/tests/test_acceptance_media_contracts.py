"""Acceptance coverage for the two media contracts reported by users.

These tests deliberately exercise public CLI/backend behavior rather than
implementation helpers: explicit image paths must contain the format named by
their suffix, and durable history media IDs must remain usable by video
start/reference inputs after a backend restart.
"""

import asyncio
import base64
import io
import os
import struct
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import main
from flow_server import history as history_store
from flow_server import media_history
from flow_server.idempotency import clear_idempotency_store_cache
from flow_server.jobs import clear_job_store_cache
from flow_server.media_types import sniff_media_type
from flow_server.models import VideoGenerationRequest
from flow_server.routes import generation


# Valid 8x6 images emitted by real PNG/JPEG encoders.  Keeping fixtures inline
# makes signature/dimension verification independent of Pillow or OS tools.
PNG_8X6 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAGCAIAAABxZ0isAAAACXBIWXMAAAABAAAAAQBP"
    "JcTWAAAAEklEQVR4nGP8w4AdsOAQp4sEAJm0ARInmA0VAAAAAElFTkSuQmCC"
)
JPEG_8X6 = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMgD/2wBDAAgEBAQE"
    "BAUFBQUFBQYGBgYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoK"
    "CgwMCwsODg4RERT/xABLAAEBAAAAAAAAAAAAAAAAAAAABgEBAAAAAAAAAAAAAAAAAA"
    "AABhABAAAAAAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIAAYACAMB"
    "IgACEQADEQD/2gAMAwEAAhEDEQA/AIsAUCX/2Q=="
)
WEBP_1X1 = base64.b64decode(
    "UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA"
)
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"acceptance-video"


class _DownloadResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def _dimensions(data: bytes) -> tuple[int, int]:
    """Read dimensions from PNG, JPEG, or WebP container headers."""
    mime = sniff_media_type(data)
    if mime == "image/png":
        return struct.unpack(">II", data[16:24])
    if mime == "image/jpeg":
        offset = 2
        sof_markers = {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        }
        while offset + 9 <= len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            marker = data[offset]
            offset += 1
            if marker in sof_markers:
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                return width, height
            if marker in {0xD8, 0xD9}:
                continue
            segment_size = struct.unpack(">H", data[offset:offset + 2])[0]
            offset += segment_size
        raise AssertionError("JPEG fixture/result has no SOF dimensions")
    if mime == "image/webp":
        chunk = data[12:16]
        payload = data[20:]
        if chunk == b"VP8 ":
            assert payload[3:6] == b"\x9d\x01\x2a"
            width, height = struct.unpack("<HH", payload[6:10])
            return width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8X":
            return int.from_bytes(payload[4:7], "little") + 1, int.from_bytes(payload[7:10], "little") + 1
        if chunk == b"VP8L":
            packed = int.from_bytes(payload[1:5], "little")
            return (packed & 0x3FFF) + 1, ((packed >> 14) & 0x3FFF) + 1
        raise AssertionError(f"Unsupported WebP chunk {chunk!r}")
    raise AssertionError(f"Not a supported image: {mime}")


@pytest.mark.parametrize(
    ("source", "output_suffix", "expected_mime", "expected_size"),
    [
        (JPEG_8X6, ".png", "image/png", (8, 6)),
        (PNG_8X6, ".jpg", "image/jpeg", (8, 6)),
        (PNG_8X6, ".webp", "image/webp", (8, 6)),
    ],
)
def test_explicit_image_output_is_reencoded_to_true_format_with_dimensions(
    monkeypatch, tmp_path, source, output_suffix, expected_mime, expected_size
):
    destination = tmp_path / f"exact-output{output_suffix}"
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _DownloadResponse(source),
    )

    actual_path = main._download_media(
        "https://media.invalid/generated", str(destination), preserve_explicit=True
    )
    result = destination.read_bytes()

    assert actual_path == str(destination)
    assert sniff_media_type(result) == expected_mime
    assert _dimensions(result) == expected_size
    assert not list(tmp_path.glob("*.flow-part-*"))
    assert not list(tmp_path.glob("*.flow-convert-*"))


def test_failed_image_conversion_is_atomic_and_preserves_existing_output(monkeypatch, tmp_path):
    destination = tmp_path / "existing.png"
    destination.write_bytes(PNG_8X6)
    original = destination.read_bytes()
    # Signature is JPEG, but the truncated stream cannot be decoded/re-encoded.
    broken_jpeg = b"\xff\xd8\xff\xe0\x00\x10broken"
    monkeypatch.setattr(
        main.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _DownloadResponse(broken_jpeg),
    )

    with pytest.raises(SystemExit) as caught:
        main._download_media(
            "https://media.invalid/broken", str(destination), preserve_explicit=True
        )

    assert caught.value.code == main.EXIT_OUTPUT_ERROR
    assert destination.read_bytes() == original
    assert not [path for path in tmp_path.iterdir() if path != destination]


def _configure_history(monkeypatch, tmp_path):
    output_dir = tmp_path / "output"
    history_path = output_dir / "history.json"
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(history_path))
    monkeypatch.setattr(media_history, "OUTPUT_DIR", str(output_dir))
    monkeypatch.setattr(generation, "OUTPUT_DIR", str(output_dir))
    clear_idempotency_store_cache()
    clear_job_store_cache()
    return output_dir, history_path


@pytest.mark.parametrize(
    ("arguments", "expected_fields"),
    [
        (["--start", "generated-image-id"], {"start_media_id": "generated-image-id"}),
        (["--start", "uploaded-image-id"], {"start_media_id": "uploaded-image-id"}),
        (["--ref", "generated-image-id"], {"ref_media_ids": ["generated-image-id"]}),
        (["--ref", "uploaded-image-id"], {"ref_media_ids": ["uploaded-image-id"]}),
        (
            ["--start", "generated-image-id", "--end", "uploaded-image-id"],
            {
                "start_media_id": "generated-image-id",
                "end_media_id": "uploaded-image-id",
            },
        ),
    ],
)
def test_cli_forwards_exact_generated_and_uploaded_ids_for_start_ref_and_end(
    monkeypatch, arguments, expected_fields
):
    captured = {}
    monkeypatch.setattr(main, "_wait_for_generation_ready", lambda: {})

    def post_generation(path, payload, idempotency_key, timeout):
        captured.update(
            path=path,
            payload=payload,
            idempotency_key=idempotency_key,
            timeout=timeout,
        )
        return {
            "job_id": "acceptance-job",
            "status": "succeeded",
            "created": 1,
            "data": [{"url": "http://test/video.mp4", "media_id": "video-id"}],
        }

    monkeypatch.setattr(main, "_post_generation", post_generation)
    monkeypatch.setattr(main, "_save_outputs", lambda *args, **kwargs: [])

    main.cmd_video(
        ["acceptance prompt", *arguments, "--idempotency-key", "exact-id-key"]
    )

    assert captured["path"] == "/v1/videos/generations"
    assert captured["idempotency_key"] == "exact-id-key"
    for field, expected_value in expected_fields.items():
        assert captured["payload"][field] == expected_value


class _HistoryBridge:
    def _select_client_for_cost(self, _cost):
        return None

    async def api_request(self, endpoint, body=None, **kwargs):
        if endpoint == "/v1/credits":
            return {"status": 200, "data": {"credits": 1000}}
        if endpoint in {"/v1/media/generated-image-id", "/v1/media/uploaded-image-id"}:
            return {"status": 200, "data": {}}
        if endpoint == "/v1/media/nonexistent-media-id":
            return {"status": 404, "data": {"error": {"message": "not found"}}}
        raise AssertionError(f"Unexpected bridge request: {endpoint}")


async def _publish(filename, _path):
    return f"http://test/download/{filename}", None


async def _download_video(_bridge, _media_id, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_bytes(MP4_BYTES)
    return True


def test_generated_and_uploaded_history_ids_drive_video_after_restart_and_retry_once(
    monkeypatch, tmp_path
):
    output_dir, history_path = _configure_history(monkeypatch, tmp_path)
    output_dir.mkdir(parents=True)
    generated_path = output_dir / "generated.png"
    generated_path.write_bytes(PNG_8X6)
    uploaded_path = tmp_path / "uploaded.jpg"
    uploaded_path.write_bytes(JPEG_8X6)
    generated_record = media_history.record_local_media(
        generated_path,
        media_id="generated-image-id",
        mime_type="image/png",
        source="generated",
    )
    media_history.record_uploaded_media(
        uploaded_path,
        "uploaded-image-id",
        mime_type="image/jpeg",
    )

    # Simulate a backend restart: discard module/store caches and resolve only
    # from the durable history.json file.
    clear_idempotency_store_cache()
    clear_job_store_cache()

    start_submit = AsyncMock(
        side_effect=[["generated-start-video-id"], ["uploaded-start-video-id"]]
    )
    ref_submit = AsyncMock(
        side_effect=[["generated-ref-video-id"], ["uploaded-ref-video-id"]]
    )

    async def scenario():
        bridge = _HistoryBridge()
        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=bridge)),
            patch("flow_engine.generators.i2v.generate_video_i2v", start_submit),
            patch("flow_engine.generators.i2v.generate_video_r2v", ref_submit),
            patch("flow_engine.generators.common.poll_status", AsyncMock(return_value=True)),
            patch("flow_engine.generators.common.download_video", _download_video),
            patch.object(generation, "publish", _publish),
        ):
            start_request = VideoGenerationRequest(
                prompt="animate generated image",
                start_media_id=generated_record["media_id"],
            )
            first = await generation.openai_generate_video(
                start_request, None, "same-start-idempotency-key"
            )
            retry = await generation.openai_generate_video(
                start_request, None, "same-start-idempotency-key"
            )
            uploaded_start_result = await generation.openai_generate_video(
                VideoGenerationRequest(
                    prompt="animate uploaded image",
                    start_media_id="uploaded-image-id",
                ),
                None,
                "uploaded-start-idempotency-key",
            )
            generated_ref_result = await generation.openai_generate_video(
                VideoGenerationRequest(
                    prompt="use generated reference",
                    ref_media_ids=["generated-image-id"],
                ),
                None,
                "generated-reference-idempotency-key",
            )
            uploaded_ref_result = await generation.openai_generate_video(
                VideoGenerationRequest(
                    prompt="use uploaded reference",
                    ref_media_ids=["uploaded-image-id"],
                ),
                None,
                "uploaded-reference-idempotency-key",
            )
            return first, retry, uploaded_start_result, generated_ref_result, uploaded_ref_result

    first, retry, uploaded_start_result, generated_ref_result, uploaded_ref_result = asyncio.run(scenario())

    assert first == retry
    assert start_submit.await_count == 2
    assert [call.args[4] for call in start_submit.await_args_list] == [
        "generated-image-id",
        "uploaded-image-id",
    ]
    assert ref_submit.await_count == 2
    assert [call.args[4] for call in ref_submit.await_args_list] == [
        ["generated-image-id"],
        ["uploaded-image-id"],
    ]
    for result in (uploaded_start_result, generated_ref_result, uploaded_ref_result):
        assert result["status"] == "succeeded"
    persisted = history_store.load_history(path=history_path)["history"]
    persisted_ids = {entry.get("media_id") for entry in persisted}
    assert {
        "generated-image-id",
        "uploaded-image-id",
        "generated-start-video-id",
        "uploaded-start-video-id",
        "generated-ref-video-id",
        "uploaded-ref-video-id",
    } <= persisted_ids
    assert not list(tmp_path.rglob("media-id.js"))
    assert not list(tmp_path.rglob("media-ids.json"))


def test_nonexistent_raw_media_id_returns_http_404(monkeypatch, tmp_path):
    _configure_history(monkeypatch, tmp_path)
    bridge = _HistoryBridge()
    submit = AsyncMock(side_effect=AssertionError("generation must not run for a missing ID"))

    async def scenario():
        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=bridge)),
            patch("flow_engine.generators.i2v.generate_video_i2v", submit),
        ):
            return await generation._generate_video(
                VideoGenerationRequest(
                    prompt="must fail",
                    start_media_id="nonexistent-media-id",
                )
            )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(scenario())

    assert caught.value.status_code == 404
    assert "nonexistent-media-id" in str(caught.value.detail)
    assert submit.await_count == 0


def test_start_and_end_exact_history_media_ids_use_first_last_frame_generation(
    monkeypatch, tmp_path
):
    output_dir, _history_path = _configure_history(monkeypatch, tmp_path)
    output_dir.mkdir(parents=True)
    start_path = output_dir / "generated-start.png"
    end_path = output_dir / "uploaded-end.jpg"
    start_path.write_bytes(PNG_8X6)
    end_path.write_bytes(JPEG_8X6)
    media_history.record_local_media(
        start_path,
        media_id="generated-image-id",
        project_id="project-a",
        source="generated",
    )
    media_history.record_uploaded_media(
        end_path,
        "uploaded-image-id",
        project_id="project-a",
    )
    submit = AsyncMock(return_value=["first-last-video-id"])

    async def scenario():
        with (
            patch.object(
                generation, "get_active_bridge", AsyncMock(return_value=_HistoryBridge())
            ),
            patch("flow_engine.generators.i2v.generate_video_fl", submit),
            patch(
                "flow_engine.generators.common.poll_status", AsyncMock(return_value=True)
            ),
            patch("flow_engine.generators.common.download_video", _download_video),
            patch.object(generation, "publish", _publish),
        ):
            return await generation.openai_generate_video(
                VideoGenerationRequest(
                    prompt="transition between exact history IDs",
                    start_media_id="generated-image-id",
                    end_media_id="uploaded-image-id",
                ),
                None,
                "first-last-idempotency-key",
            )

    result = asyncio.run(scenario())

    assert result["status"] == "succeeded"
    assert submit.await_count == 1
    assert submit.await_args.kwargs["start_image_id"] == "generated-image-id"
    assert submit.await_args.kwargs["end_image_id"] == "uploaded-image-id"
    assert submit.await_args.kwargs["count"] == 1


def test_stale_exact_id_is_reuploaded_once_and_history_is_refreshed(monkeypatch, tmp_path):
    _output_dir, history_path = _configure_history(monkeypatch, tmp_path)
    local_image = tmp_path / "stale-reference.png"
    local_image.write_bytes(PNG_8X6)
    original = media_history.record_uploaded_media(
        local_image,
        "stale-image-id",
        project_id="project-a",
        mime_type="image/png",
    )

    class StaleBridge:
        def __init__(self):
            self.stale_checks = 0
            self.fresh_checks = 0
            self.upload_calls = 0

        async def api_request(self, endpoint, body=None, **kwargs):
            if endpoint == "/v1/media/stale-image-id":
                self.stale_checks += 1
                return {"status": 404, "data": {"error": {"message": "not found"}}}
            if endpoint == "/v1/media/refreshed-image-id":
                self.fresh_checks += 1
                return {"status": 200, "data": {}}
            if endpoint == "/v1/flow/uploadImage":
                self.upload_calls += 1
                return {"status": 200, "data": {"mediaId": "refreshed-image-id"}}
            raise AssertionError(f"Unexpected bridge request: {endpoint}")

    bridge = StaleBridge()

    async def scenario():
        first = await media_history.resolve_media_reference(
            "stale-image-id",
            bridge,
            expected_type="image",
            project_id="project-a",
        )
        retry = await media_history.resolve_media_reference(
            "refreshed-image-id",
            bridge,
            expected_type="image",
            project_id="project-a",
        )
        return first, retry

    first, retry = asyncio.run(scenario())

    assert first == retry == "refreshed-image-id"
    assert bridge.stale_checks == 1
    assert bridge.upload_calls == 1
    assert bridge.fresh_checks == 1
    registry = history_store.HistoryRegistry(history_path, legacy_path=None)
    refreshed = registry.require(filename=original["filename"])
    assert refreshed["media_id"] == "refreshed-image-id"
    assert refreshed["fingerprint"] == original["fingerprint"]
    assert registry.find(media_id="stale-image-id") is None
    assert not list(tmp_path.rglob("media-id.js"))
    assert not list(tmp_path.rglob("media-ids.json"))
