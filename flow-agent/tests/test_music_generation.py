"""Comprehensive test suite for Flow Agent music/audio generation capabilities."""

import base64
import os
import tempfile
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from flow_server.media_types import (
    extension_for_media,
    extension_for_mime,
    sniff_media_type,
)
from flow_engine.generators.t2m import (
    _parse_music_results,
    build_music_client_context,
    download_music,
    generate_music,
)
from flow_server.models import MusicGenerationRequest


# ─── 1. Media Type & Audio Sniffing Tests ─────────────────────────

def test_audio_mime_extensions():
    assert extension_for_mime("audio/mpeg") == ".mp3"
    assert extension_for_mime("audio/mp3") == ".mp3"
    assert extension_for_mime("audio/wav") == ".wav"
    assert extension_for_mime("audio/x-wav") == ".wav"
    assert extension_for_mime("audio/ogg") == ".ogg"
    assert extension_for_mime("audio/flac") == ".flac"
    assert extension_for_mime("audio/aac") == ".aac"
    assert extension_for_mime("audio/mp4") == ".m4a"
    assert extension_for_mime("audio/x-m4a") == ".m4a"


def test_audio_signature_sniffing():
    # MP3 with ID3 tag
    id3_header = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 50
    assert sniff_media_type(id3_header) == "audio/mpeg"

    # MP3 sync frame
    mp3_frame = b"\xff\xfb\x90\x44" + b"\x00" * 50
    assert sniff_media_type(mp3_frame) == "audio/mpeg"

    # WAV header
    wav_header = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
    assert sniff_media_type(wav_header) == "audio/wav"

    # OGG header
    ogg_header = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00"
    assert sniff_media_type(ogg_header) == "audio/ogg"

    # FLAC header
    flac_header = b"fLaC\x00\x00\x00\x22"
    assert sniff_media_type(flac_header) == "audio/flac"

    # AAC sync word
    aac_header = b"\xff\xf1\x50\x80"
    assert sniff_media_type(aac_header) == "audio/aac"

    # M4A container
    m4a_header = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom"
    assert sniff_media_type(m4a_header) == "audio/mp4"


def test_extension_for_audio_media():
    wav_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
    assert extension_for_media(wav_bytes) == ".wav"

    id3_bytes = b"ID3\x03\x00\x00\x00\x00\x00\x00"
    assert extension_for_media(id3_bytes) == ".mp3"


# ─── 2. Engine Generator Tests ────────────────────────────────────

def test_build_music_client_context():
    ctx = build_music_client_context("test-project-123", tool="MUSIC_FX")
    assert ctx["projectId"] == "test-project-123"
    assert ctx["tool"] == "MUSIC_FX"
    assert "sessionId" in ctx
    assert "recaptchaContext" in ctx


def test_parse_music_results_sounds_format():
    test_id = str(uuid.uuid4())
    data = {
        "sounds": [
            {
                "name": test_id,
                "audio": base64.b64encode(b"ID3mockaudio").decode("utf-8"),
                "fifeUrl": f"https://example.com/audio/{test_id}",
                "revisedPrompt": "An epic cinematic ambient soundtrack",
            }
        ]
    }
    parsed = _parse_music_results(data, "cinematic music")
    assert len(parsed) == 1
    assert parsed[0]["media_id"] == test_id
    assert parsed[0]["audio_url"] == f"https://example.com/audio/{test_id}"
    assert parsed[0]["revised_prompt"] == "An epic cinematic ambient soundtrack"


def test_parse_music_results_media_format():
    test_id = str(uuid.uuid4())
    data = {
        "media": [
            {
                "name": test_id,
                "audio": {
                    "generatedAudio": {
                        "downloadUrl": f"https://example.com/download/{test_id}",
                        "rawBytes": base64.b64encode(b"ID3mockbytes").decode("utf-8"),
                    }
                },
            }
        ]
    }
    parsed = _parse_music_results(data, "ambient loop")
    assert len(parsed) == 1
    assert parsed[0]["media_id"] == test_id
    assert parsed[0]["audio_url"] == f"https://example.com/download/{test_id}"


@pytest.mark.asyncio
async def test_generate_music_success():
    mock_bridge = MagicMock()
    test_id = str(uuid.uuid4())
    mock_bridge.api_request = AsyncMock(
        return_value={
            "status": 200,
            "data": {
                "sounds": [
                    {
                        "name": test_id,
                        "audio": base64.b64encode(b"ID3mockaudio").decode("utf-8"),
                        "downloadUrl": f"https://example.com/{test_id}.mp3",
                    }
                ]
            },
        }
    )

    results = await generate_music(
        mock_bridge,
        prompt="Cyberpunk beat",
        project_id="p-1",
        duration=30,
        count=1,
        loop=True,
    )

    assert results is not None
    assert len(results) == 1
    assert results[0]["media_id"] == test_id
    mock_bridge.api_request.assert_called_once()


@pytest.mark.asyncio
async def test_download_music_from_base64():
    mock_bridge = MagicMock()
    b64_audio = base64.b64encode(b"ID3mockaudiobytes" + b"\x00" * 30).decode("utf-8")

    with tempfile.TemporaryDirectory() as tmpdir:
        out_file = os.path.join(tmpdir, "output.mp3")
        saved_path = await download_music(
            mock_bridge,
            media_id_or_url="test-id",
            output_path=out_file,
            audio_base64=b64_audio,
        )

        assert saved_path is not None
        assert os.path.exists(saved_path)
        with open(saved_path, "rb") as f:
            content = f.read()
        assert content.startswith(b"ID3")


# ─── 3. Pydantic Models & API Route Tests ─────────────────────────

def test_music_generation_request_validation():
    req = MusicGenerationRequest(prompt="Relaxing piano", duration=30, loop=True, n=2)
    assert req.prompt == "Relaxing piano"
    assert req.duration == 30
    assert req.loop is True
    assert req.n == 2
    assert req.response_format == "url"


@pytest.mark.asyncio
async def test_api_audio_generations_endpoint():
    from fastapi.testclient import TestClient
    from flow_server.api import app
    from flow_server.state import set_bridge

    mock_bridge = MagicMock()
    mock_bridge.health_check = AsyncMock(return_value=True)
    test_id = str(uuid.uuid4())
    b64_audio = base64.b64encode(b"ID3mockaudiobytes" + b"\x00" * 30).decode("utf-8")
    mock_bridge.api_request = AsyncMock(
        return_value={
            "status": 200,
            "data": {
                "sounds": [
                    {
                        "name": test_id,
                        "audio": b64_audio,
                        "downloadUrl": f"https://example.com/{test_id}.mp3",
                        "revisedPrompt": "Relaxing piano melody",
                    }
                ]
            },
        }
    )
    set_bridge(mock_bridge)

    client = TestClient(app)
    response = client.post(
        "/v1/audio/generations",
        json={"prompt": "Relaxing piano", "duration": 30, "loop": False, "n": 1},
    )

    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    assert len(data["data"]) == 1
    assert data["data"][0]["media_id"] == test_id
    assert data["data"][0]["revised_prompt"] == "Relaxing piano melody"


# ─── 4. MCP Tool Tests ────────────────────────────────────────────

def test_mcp_server_music_tool_registered():
    from flow_server.mcp_server import handle_tools_list

    response = handle_tools_list("req-1")
    tools = response.get("result", {}).get("tools", [])
    tool_names = [t["name"] for t in tools]
    assert "generate_flow_music" in tool_names

    music_tool = next(t for t in tools if t["name"] == "generate_flow_music")
    assert "prompt" in music_tool["inputSchema"]["required"]
    assert "duration" in music_tool["inputSchema"]["properties"]
    assert "loop" in music_tool["inputSchema"]["properties"]


# ─── 5. Flow Music Pipeline & Fallback Tests ──────────────────────

@pytest.mark.asyncio
async def test_flowmusic_pipeline_success():
    from flow_engine.generators.t2m import _generate_flowmusic

    clip_id = str(uuid.uuid4())
    mock_bridge = MagicMock()

    async def mock_api_request(url, payload, method="POST", timeout=None, **kwargs):
        if "conversation" in url:
            return {"status": 200, "data": {"job_id": "job-123"}}
        if "stream" in url:
            return {"status": 200, "data": {"clip_ids": [clip_id]}}
        if "clips" in url:
            return {
                "status": 200,
                "data": {
                    "clips": {
                        clip_id: {
                            "audio_url": f"https://flowmusic.app/audio/{clip_id}.m4a",
                            "title": "Summer Lo-Fi",
                            "duration": {"value": 30.0},
                        }
                    }
                },
            }
        return {"status": 404, "error": "NOT_FOUND"}

    mock_bridge.api_request = AsyncMock(side_effect=mock_api_request)

    results = await _generate_flowmusic(mock_bridge, "relaxing lo-fi beat")
    assert results is not None
    assert len(results) == 1
    assert results[0]["media_id"] == clip_id
    assert results[0]["audio_url"] == f"https://flowmusic.app/audio/{clip_id}.m4a"


@pytest.mark.asyncio
async def test_generate_music_fallback_on_flowmusic_error():
    mock_bridge = MagicMock()
    test_id = str(uuid.uuid4())

    # Flow music fails, then Google Labs endpoint succeeds
    async def mock_api_request(url, payload, **kwargs):
        if "flowmusic.app" in url:
            return {"status": 500, "error": "SERVICE_UNAVAILABLE"}
        return {
            "status": 200,
            "data": {
                "sounds": [
                    {
                        "name": test_id,
                        "audio": base64.b64encode(b"ID3mockaudio").decode("utf-8"),
                        "downloadUrl": f"https://example.com/{test_id}.mp3",
                    }
                ]
            },
        }

    mock_bridge.api_request = AsyncMock(side_effect=mock_api_request)

    results = await generate_music(
        mock_bridge,
        prompt="Calm piano",
        project_id="test-proj",
        duration=30,
    )
    assert results is not None
    assert len(results) == 1
    assert results[0]["media_id"] == test_id
