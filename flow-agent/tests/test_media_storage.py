import asyncio
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from flow_engine import media_store
from flow_engine.generators.i2v import generate_video_i2v, upload_image
from flow_server import history as history_store
from flow_server import media_history
from flow_server.history import HistoryRegistry, MediaNotFoundError
from flow_server.media_types import (
    ensure_correct_extension,
    extension_for_media,
    sniff_media_type,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-png-payload"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"test-jpeg-payload"
MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp41"
WEBM_BYTES = b"\x1a\x45\xdf\xa3\x42\x82\x84webm" + b"test-webm-payload"


def _configure_store(monkeypatch, tmp_path):
    store_path = tmp_path / "output" / "history.json"
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(store_path))
    monkeypatch.setattr(media_history, "OUTPUT_DIR", str(store_path.parent))
    monkeypatch.setattr(media_store, "HISTORY_FILE", str(store_path))
    monkeypatch.setattr(media_store, "MEDIA_ID_FILE", str(store_path))
    monkeypatch.setattr(media_store, "LEGACY_MEDIA_ID_FILE", str(tmp_path / "missing-legacy.js"))
    return store_path


def test_signature_wins_over_filename_and_declared_mime(tmp_path):
    disguised_image = tmp_path / "actually-jpeg.png"
    disguised_image.write_bytes(JPEG_BYTES)

    assert sniff_media_type(
        disguised_image, declared_mime="image/png"
    ) == "image/jpeg"
    assert extension_for_media(disguised_image) == ".jpg"
    assert sniff_media_type(MP4_BYTES, declared_mime="image/png") == "video/mp4"
    assert sniff_media_type(WEBM_BYTES, filename="wrong.mp4") == "video/webm"


def test_auto_file_extension_is_corrected_but_explicit_path_is_preserved(tmp_path):
    auto_path = tmp_path / "generated.png"
    auto_path.write_bytes(WEBM_BYTES)
    corrected = ensure_correct_extension(auto_path)

    assert corrected == str(tmp_path / "generated.webm")
    assert Path(corrected).read_bytes() == WEBM_BYTES
    assert not auto_path.exists()

    explicit_path = tmp_path / "requested-name.bin"
    explicit_path.write_bytes(PNG_BYTES)
    preserved = ensure_correct_extension(explicit_path, preserve_explicit=True)
    assert preserved == str(explicit_path)
    assert explicit_path.read_bytes() == PNG_BYTES


def test_flow_output_dir_override_is_shared(tmp_path):
    configured_output = tmp_path / "custom-output"
    env = os.environ.copy()
    env["FLOW_OUTPUT_DIR"] = str(configured_output)
    env["OUTPUT_DIR"] = str(tmp_path / "legacy-must-not-win")
    command = [
        sys.executable,
        "-c",
        (
            "from flow_engine.config import OUTPUT_DIR as engine_output; "
            "from flow_server.config import OUTPUT_DIR as server_output; "
            "print(engine_output); print(server_output)"
        ),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [str(configured_output), str(configured_output)]
    assert configured_output.is_dir()


def test_default_output_is_beside_flow_binary(tmp_path):
    binary = tmp_path / "bin" / "flow"
    env = os.environ.copy()
    env.pop("FLOW_OUTPUT_DIR", None)
    env.pop("OUTPUT_DIR", None)
    command = [
        sys.executable,
        "-c",
        (
            "import sys; "
            f"sys.argv[0] = {str(binary)!r}; "
            "from flow_engine.config import BINARY_DIR, OUTPUT_DIR; "
            "print(BINARY_DIR); print(OUTPUT_DIR)"
        ),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        str(binary.parent),
        str(binary.parent / "output"),
    ]


def test_history_is_the_only_registry_and_survives_restart(monkeypatch, tmp_path):
    store_path = _configure_store(monkeypatch, tmp_path)
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "renamed.jpg"
    first_path.write_bytes(PNG_BYTES)
    second_path.write_bytes(PNG_BYTES)

    saved = media_store.save(
        first_path, "media-one", project_id="project-a", mime_type="image/png"
    )

    # A module reload is the relevant part of a backend restart for this
    # process-local code: no in-memory mapping may be required to recover it.
    importlib.reload(media_store)
    monkeypatch.setattr(media_store, "HISTORY_FILE", str(store_path))
    monkeypatch.setattr(media_store, "MEDIA_ID_FILE", str(store_path))
    monkeypatch.setattr(media_store, "LEGACY_MEDIA_ID_FILE", str(tmp_path / "missing-legacy.js"))
    recovered = media_store.get_for_file(second_path, project_id="project-a")

    assert recovered["media_id"] == "media-one"
    assert recovered["fingerprint"] == saved["fingerprint"]
    on_disk = json.loads(store_path.read_text(encoding="utf-8"))
    matching = [
        item for item in on_disk["history"]
        if item.get("fingerprint") == saved["fingerprint"]
    ]
    assert len(matching) == 1
    assert matching[0]["media_id"] == "media-one"
    assert media_store.get_for_file(second_path, project_id="different-project") is None
    assert not list(tmp_path.rglob("media-id.js"))
    assert not list(tmp_path.rglob("media-ids.json"))


class _ValidationBridge:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    async def api_request(self, endpoint, body, **kwargs):
        self.calls.append((endpoint, body, kwargs))
        return {"status": self.status, "data": {}}


def test_cached_media_id_is_revalidated_after_restart(monkeypatch, tmp_path):
    _configure_store(monkeypatch, tmp_path)
    image_path = tmp_path / "reference.png"
    image_path.write_bytes(PNG_BYTES)
    saved = media_store.save(image_path, "persisted-id", project_id="project-a")

    healthy_bridge = _ValidationBridge(status=200)
    reused = asyncio.run(
        media_store.get_revalidated_media_id(
            image_path, healthy_bridge, project_id="project-a"
        )
    )

    assert reused == "persisted-id"
    assert healthy_bridge.calls[0][0] == "/v1/media/persisted-id"
    record = media_store.get_for_file(image_path, project_id="project-a")
    assert record["validated_at"]
    assert record["fingerprint"] == saved["fingerprint"]


def test_stale_media_id_is_invalidated_and_uploaded_once(monkeypatch, tmp_path):
    _configure_store(monkeypatch, tmp_path)
    image_path = tmp_path / "reference.png"
    image_path.write_bytes(PNG_BYTES)
    media_store.save(image_path, "stale-id", project_id="project-a")

    class Bridge:
        def __init__(self):
            self.validation_calls = 0
            self.upload_calls = 0

        async def api_request(self, endpoint, body, **kwargs):
            if endpoint.startswith("/v1/media/"):
                self.validation_calls += 1
                if endpoint.endswith("/stale-id"):
                    return {"status": 404, "data": {"error": {"message": "not found"}}}
                return {"status": 200, "data": {}}
            if endpoint == "/v1/flow/uploadImage":
                self.upload_calls += 1
                return {"status": 200, "data": {"mediaId": "fresh-id"}}
            raise AssertionError(f"unexpected endpoint {endpoint}")

    bridge = Bridge()
    first_result = asyncio.run(upload_image(bridge, str(image_path), "project-a"))
    second_result = asyncio.run(upload_image(bridge, str(image_path), "project-a"))

    assert first_result == second_result == "fresh-id"
    assert bridge.upload_calls == 1
    assert bridge.validation_calls == 2
    assert media_store.get_for_file(image_path, project_id="project-a")["media_id"] == "fresh-id"


def test_upload_is_reused_as_image_to_video_reference_without_duplicates(monkeypatch, tmp_path):
    history_path = _configure_store(monkeypatch, tmp_path)
    image_path = tmp_path / "start-frame.png"
    image_path.write_bytes(PNG_BYTES)

    class Bridge:
        def __init__(self):
            self.upload_calls = 0
            self.i2v_calls = 0

        async def api_request(self, endpoint, body, **kwargs):
            if endpoint == "/v1/flow/uploadImage":
                self.upload_calls += 1
                return {"status": 200, "data": {"mediaId": "start-media-id"}}
            if endpoint == "/v1/media/start-media-id":
                return {"status": 200, "data": {}}
            if endpoint == "/v1/video:batchAsyncGenerateVideoStartImage":
                self.i2v_calls += 1
                assert body["requests"][0]["startImage"] == {"mediaId": "start-media-id"}
                return {
                    "status": 200,
                    "data": {"media": [{"name": "generated-video-id"}]},
                }
            raise AssertionError(f"unexpected endpoint {endpoint}")

    bridge = Bridge()

    async def run_scenario():
        first_id = await upload_image(bridge, str(image_path), "project-a")
        record = history_store.find_history(
            path=history_path, fingerprint=media_store.fingerprint(image_path)
        )
        resolved_id = await media_history.resolve_media_reference(
            record["url"], bridge, expected_type="image"
        )
        retry_id = await upload_image(bridge, str(image_path), "project-a")
        videos = await generate_video_i2v(
            bridge,
            "gentle camera move",
            "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "project-a",
            resolved_id,
            duration=8,
        )
        return first_id, retry_id, resolved_id, videos

    first_id, retry_id, resolved_id, videos = asyncio.run(run_scenario())

    assert first_id == retry_id == resolved_id == "start-media-id"
    assert videos == ["generated-video-id"]
    assert bridge.upload_calls == 1
    assert bridge.i2v_calls == 1
    history = json.loads(history_path.read_text(encoding="utf-8"))["history"]
    cached = [entry for entry in history if entry.get("media_id") == "start-media-id"]
    assert len(cached) == 1
    assert cached[0]["filename"].startswith("input_")
    assert cached[0]["filename"].endswith(".png")
    assert cached[0]["fingerprint"] == media_store.fingerprint(image_path)


def test_stale_generated_media_id_recovers_local_asset_and_refreshes_same_record(
    monkeypatch, tmp_path
):
    history_path = _configure_store(monkeypatch, tmp_path)
    output_dir = history_path.parent
    output_dir.mkdir(parents=True)
    generated_path = output_dir / "older-generated.png"
    generated_path.write_bytes(PNG_BYTES)
    history_store.upsert_history({
        "type": "image",
        "url": "http://localhost:8001/download/older-generated.png?old=1",
        "media_id": "stale-generated-id",
        "prompt": "preserve this generated prompt",
        "timestamp": 123,
    })

    class Bridge:
        def __init__(self):
            self.validation_calls = []
            self.upload_calls = 0

        async def api_request(self, endpoint, body, **kwargs):
            if endpoint.startswith("/v1/media/"):
                self.validation_calls.append(endpoint)
                if endpoint.endswith("stale-generated-id"):
                    return {"status": 404, "data": {"error": {"message": "not found"}}}
                if endpoint.endswith("refreshed-generated-id"):
                    return {"status": 200, "data": {}}
            if endpoint == "/v1/flow/uploadImage":
                self.upload_calls += 1
                return {"status": 200, "data": {"mediaId": "refreshed-generated-id"}}
            raise AssertionError(f"unexpected endpoint {endpoint}")

    bridge = Bridge()

    async def resolve_before_and_after_restart():
        first = await media_history.resolve_media_reference(
            "stale-generated-id",
            bridge,
            expected_type="image",
            project_id="current-project",
        )
        # Resolution has no process-local mapping: a second call exercises the
        # atomically refreshed history file, equivalent to a backend restart.
        second = await media_history.resolve_media_reference(
            first,
            bridge,
            expected_type="image",
            project_id="current-project",
        )
        return first, second

    first, second = asyncio.run(resolve_before_and_after_restart())

    assert first == second == "refreshed-generated-id"
    assert bridge.upload_calls == 1
    assert bridge.validation_calls == [
        "/v1/media/stale-generated-id",
        "/v1/media/refreshed-generated-id",
    ]
    records = json.loads(history_path.read_text(encoding="utf-8"))["history"]
    matching = [item for item in records if item.get("filename") == generated_path.name]
    assert len(matching) == 1
    assert matching[0]["media_id"] == "refreshed-generated-id"
    assert matching[0]["prompt"] == "preserve this generated prompt"
    assert matching[0]["timestamp"] == 123
    assert matching[0]["mime_type"] == "image/png"
    assert matching[0]["type"] == "image"
    assert matching[0]["project_id"] == "current-project"
    assert matching[0]["fingerprint"] == media_store.fingerprint(generated_path)
    assert not list(tmp_path.rglob("media-id.js"))
    assert not list(tmp_path.rglob("media-ids.json"))


def test_reference_lookup_duplicate_merge_and_missing_media_error(monkeypatch, tmp_path):
    history_path = _configure_store(monkeypatch, tmp_path)
    registry = HistoryRegistry(history_path, legacy_path=None)
    first = registry.upsert({
        "type": "image",
        "url": "http://localhost:8001/download/reference.png?token=old",
        "media_id": "reference-id",
        "fingerprint": "abc123",
        "prompt": "Uploaded reference file",
    })
    second = registry.upsert({
        "filename": "reference.png",
        "media_id": "reference-id",
        "validated_at": 1234,
    })

    assert first["filename"] == "reference.png"
    assert second["fingerprint"] == "abc123"
    assert registry.find(filename="../unsafe/reference.png")["media_id"] == "reference-id"
    assert registry.find(media_id="reference-id")["filename"] == "reference.png"
    assert registry.find(fingerprint="abc123")["media_id"] == "reference-id"
    assert len(registry.load()["history"]) == 1

    with pytest.raises(MediaNotFoundError, match=r"Media not found in history\.json.*missing-id"):
        registry.require(media_id="missing-id")
    with pytest.raises(MediaNotFoundError, match=r"Media not found in history\.json.*missing\.png"):
        asyncio.run(
            media_history.resolve_media_reference(
                "missing.png", object(), expected_type="image"
            )
        )


def test_legacy_media_id_js_migrates_once_into_history(tmp_path):
    legacy_path = tmp_path / "media-id.js"
    history_path = tmp_path / "output" / "history.json"
    legacy_path.write_text(
        "start.png : image-media-id\nclip.mp4 : video-media-id\ninvalid-line\n",
        encoding="utf-8",
    )

    first_process = HistoryRegistry(history_path, legacy_path=legacy_path)
    migrated = first_process.load()
    second_process = HistoryRegistry(history_path, legacy_path=legacy_path)
    reloaded = second_process.load()

    assert migrated["migrations"]["media_id_js"] is True
    assert reloaded == migrated
    assert len(reloaded["history"]) == 2
    assert second_process.require(filename="start.png")["media_id"] == "image-media-id"
    assert second_process.require(media_id="video-media-id")["filename"] == "clip.mp4"
    assert not legacy_path.exists()
    assert not (tmp_path / "output" / "media-ids.json").exists()
    assert not (tmp_path / "output" / "media-id.js").exists()


def test_history_write_uses_atomic_replace(monkeypatch, tmp_path):
    history_path = tmp_path / "output" / "history.json"
    registry = HistoryRegistry(history_path, legacy_path=None, legacy_store_path=None)
    real_replace = os.replace
    replacements = []

    def capture_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.parent == history_path.parent
        assert source_path.name.startswith(".history-")
        assert source_path.is_file()
        replacements.append((source_path, destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(history_store.os, "replace", capture_replace)
    registry.upsert({
        "url": "https://example.invalid/download/atomic.png",
        "media_id": "atomic-media-id",
        "type": "image",
        "prompt": "atomic history test",
        "timestamp": 123,
    })

    assert replacements
    assert replacements[-1][1] == history_path
    assert history_path.is_file()
    assert not list(history_path.parent.glob(".history-*.tmp"))


def test_default_runtime_creates_no_nested_or_cache_registries(tmp_path):
    binary_path = tmp_path / "bin" / "flow"
    binary_path.parent.mkdir()
    binary_path.write_text("", encoding="utf-8")
    source_path = tmp_path / "reference.bin"
    source_path.write_bytes(PNG_BYTES)
    env = os.environ.copy()
    for name in ("FLOW_OUTPUT_DIR", "OUTPUT_DIR", "FLOW_HISTORY_FILE", "FLOW_MEDIA_STORE"):
        env.pop(name, None)
    command = [
        sys.executable,
        "-c",
        (
            "import sys; "
            f"sys.argv[0] = {str(binary_path)!r}; "
            "from flow_engine import media_store; "
            f"media_store.save({str(source_path)!r}, 'persisted-id', mime_type='image/png'); "
            "from flow_engine.config import HISTORY_FILE; print(HISTORY_FILE)"
        ),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    expected_history = binary_path.parent / "output" / "history.json"
    assert result.stdout.strip() == str(expected_history)
    assert expected_history.is_file()
    assert not list(tmp_path.rglob("media-id.js"))
    assert not list(tmp_path.rglob("media-ids.json"))
    for forbidden in (
        binary_path.parent / "input",
        binary_path.parent / "cache",
        binary_path.parent / ".cache",
        binary_path.parent / "output" / "input",
        binary_path.parent / "output" / "output",
        binary_path.parent / "output" / "cache",
        binary_path.parent / "output" / ".cache",
    ):
        assert not forbidden.exists(), f"unexpected runtime path created: {forbidden}"
