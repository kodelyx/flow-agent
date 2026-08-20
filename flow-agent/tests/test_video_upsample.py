import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from flow_engine.config import ENDPOINTS
from flow_engine.generators import upsample as upsample_mod
from flow_engine.generators.upsample import normalise_resolution, upsample_video
from flow_server.idempotency import clear_idempotency_store_cache
from flow_server.jobs import clear_job_store_cache
from flow_server.models import VideoGenerationRequest, VideoUpsampleRequest
from flow_server.routes import generation


MP4 = b"\x00\x00\x00\x18ftypisom" + b"test-video"


class RecordingBridge:
    """Bridge stub that records every upsample submission it is handed."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _select_client_for_cost(self, _cost):
        return None

    async def api_request(self, path, body=None, captcha_action=None, method="POST"):
        if path == "/v1/credits":
            return {"data": {"credits": 1000}}
        self.calls.append({
            "path": path,
            "body": body,
            "captcha_action": captcha_action,
            "method": method,
        })
        return self.responses.pop(0)


def _ok(media_ids):
    return {"status": 200, "data": {"media": [{"name": mid} for mid in media_ids]}}


def _rejected(message):
    return {
        "status": 400,
        "data": {"error": {"message": message, "details": [{"reason": "INVALID_ARGUMENT"}]}},
    }


async def _publish(filename, _path):
    return f"http://test/download/{filename}", None


async def _append_history(*_args, **_kwargs):
    return None


async def _download(_bridge, _media_id, path):
    with open(path, "wb") as handle:
        handle.write(MP4)
    return True


class ResolutionAliasTests(unittest.TestCase):
    def test_native_and_higher_resolutions_map_to_tiers(self):
        for native in (None, "", "720p", "720", "native", "original"):
            self.assertIsNone(normalise_resolution(native))
        for value in ("1080p", "1080", "FHD", "Full HD"):
            self.assertEqual(normalise_resolution(value), "1080p")
        for value in ("4k", "4K", "2160p", "uhd"):
            self.assertEqual(normalise_resolution(value), "4k")

    def test_unknown_resolution_is_rejected_before_any_request(self):
        with self.assertRaises(ValueError) as exc:
            normalise_resolution("8k")
        self.assertIn("720p", str(exc.exception))


class UpsampleRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_targets_the_upsampler_endpoint_and_model(self):
        bridge = RecordingBridge([_ok(["upsampled-1"])])

        media_ids = await upsample_video(
            bridge,
            "source-media-1",
            "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "project-1",
            resolution="1080p",
            seed=7,
        )

        self.assertEqual(media_ids, ["upsampled-1"])
        self.assertEqual(len(bridge.calls), 1)
        call = bridge.calls[0]
        self.assertEqual(call["path"], ENDPOINTS["upsample_video"])
        self.assertEqual(call["path"], "/v1/video:batchAsyncGenerateVideoUpsampleVideo")
        self.assertEqual(call["captcha_action"], "VIDEO_GENERATION")

        item = call["body"]["requests"][0]
        self.assertEqual(item["videoModelKey"], "veo_3_1_upsampler_1080p")
        self.assertEqual(item["resolution"], "VIDEO_RESOLUTION_1080P")
        self.assertEqual(item["videoInput"], {"mediaId": "source-media-1"})
        self.assertEqual(item["aspectRatio"], "VIDEO_ASPECT_RATIO_LANDSCAPE")
        self.assertEqual(item["seed"], 7)
        self.assertEqual(
            call["body"]["clientContext"]["projectId"], "project-1"
        )

    async def test_4k_uses_the_4k_upsampler_model(self):
        bridge = RecordingBridge([_ok(["upsampled-4k"])])

        await upsample_video(bridge, "source", "VIDEO_ASPECT_RATIO_PORTRAIT", "p", resolution="4k")

        item = bridge.calls[0]["body"]["requests"][0]
        self.assertEqual(item["videoModelKey"], "veo_3_1_upsampler_4k")
        self.assertEqual(item["resolution"], "VIDEO_RESOLUTION_4K")

    async def test_rejected_resolution_enum_retries_the_next_spelling(self):
        bridge = RecordingBridge([
            _rejected("Invalid value at 'requests[0].resolution' (TYPE_ENUM)"),
            _ok(["upsampled-2"]),
        ])

        media_ids = await upsample_video(bridge, "source", "VIDEO_ASPECT_RATIO_LANDSCAPE", "p")

        self.assertEqual(media_ids, ["upsampled-2"])
        self.assertEqual(len(bridge.calls), 2)
        self.assertEqual(
            bridge.calls[0]["body"]["requests"][0]["resolution"], "VIDEO_RESOLUTION_1080P"
        )
        self.assertEqual(
            bridge.calls[1]["body"]["requests"][0]["resolution"], "VIDEO_RESOLUTION_1080p"
        )

    async def test_last_resort_attempt_omits_the_resolution_field(self):
        bridge = RecordingBridge([
            _rejected("Invalid value at 'requests[0].resolution'"),
            _rejected("Cannot find field: resolution"),
            _ok(["upsampled-3"]),
        ])

        media_ids = await upsample_video(bridge, "source", "VIDEO_ASPECT_RATIO_LANDSCAPE", "p")

        self.assertEqual(media_ids, ["upsampled-3"])
        self.assertEqual(len(bridge.calls), 3)
        # The model key alone encodes the target resolution.
        self.assertNotIn("resolution", bridge.calls[2]["body"]["requests"][0])

    async def test_a_real_failure_is_not_retried(self):
        bridge = RecordingBridge([
            {"status": 429, "data": {"error": {"message": "UNUSUAL_ACTIVITY"}}},
        ])

        with self.assertRaises(ValueError) as exc:
            await upsample_video(bridge, "source", "VIDEO_ASPECT_RATIO_LANDSCAPE", "p")

        self.assertIn("UNUSUAL_ACTIVITY", str(exc.exception))
        self.assertEqual(len(bridge.calls), 1)

    async def test_upsampling_to_native_resolution_is_refused(self):
        bridge = RecordingBridge([])
        with self.assertRaises(ValueError):
            await upsample_video(bridge, "source", "a", "p", resolution="720p")
        self.assertEqual(bridge.calls, [])

    async def test_media_ids_are_read_from_the_legacy_operations_shape(self):
        bridge = RecordingBridge([
            {"status": 200, "data": {"operations": [{"media": {"name": "legacy-1"}}]}},
        ])

        self.assertEqual(
            await upsample_video(bridge, "source", "a", "p"), ["legacy-1"]
        )


class UpsampleRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        clear_idempotency_store_cache()
        clear_job_store_cache()
        self.output_patch = patch.object(generation, "OUTPUT_DIR", self.tempdir.name)
        self.output_patch.start()

    def tearDown(self):
        self.output_patch.stop()
        clear_idempotency_store_cache()
        clear_job_store_cache()
        self.tempdir.cleanup()

    async def test_upsample_route_downloads_the_high_resolution_file_once(self):
        bridge = RecordingBridge([_ok(["upsampled-1"])])
        request = VideoUpsampleRequest(media_id="source-media-1", resolution="1080p")

        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=bridge)),
            patch.object(generation, "resolve_media_reference", AsyncMock(return_value="source-media-1")),
            patch("flow_engine.generators.common.poll_status", AsyncMock(return_value=True)),
            patch("flow_engine.generators.common.download_video", _download),
            patch.object(generation, "publish", _publish),
            patch.object(generation, "append_to_history", _append_history),
        ):
            first = await generation.openai_upsample_video(request, None, "upsample-key-1")
            retry = await generation.openai_upsample_video(request, None, "upsample-key-1")
            polled = await generation.get_video_generation(first["job_id"])

        self.assertEqual(first, retry)
        self.assertEqual(first, polled)
        self.assertEqual(first["status"], "succeeded")
        entry = first["data"][0]
        self.assertEqual(entry["resolution"], "1080p")
        self.assertEqual(entry["media_id"], "upsampled-1")
        self.assertEqual(entry["source_media_id"], "source-media-1")
        # The retry replayed the stored result instead of paying again.
        self.assertEqual(len(bridge.calls), 1)

    async def test_a_bad_resolution_is_rejected_without_calling_flow(self):
        bridge = RecordingBridge([])
        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=bridge)),
        ):
            with self.assertRaises(HTTPException) as exc:
                await generation.openai_upsample_video(
                    VideoUpsampleRequest(media_id="m", resolution="8k"), None, None
                )
        self.assertEqual(exc.exception.status_code, 400)
        self.assertEqual(bridge.calls, [])

    async def test_video_generation_at_1080p_returns_the_upsampled_file_first(self):
        bridge = RecordingBridge([_ok(["upsampled-1"])])
        request = VideoGenerationRequest(prompt="ocean waves", resolution="1080p")

        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=bridge)),
            patch("flow_engine.generators.t2v.generate_video", AsyncMock(return_value=["native-1"])),
            patch("flow_engine.generators.common.poll_status", AsyncMock(return_value=True)),
            patch("flow_engine.generators.common.download_video", _download),
            patch.object(generation, "publish", _publish),
            patch.object(generation, "append_to_history", _append_history),
        ):
            result = await generation.openai_generate_video(request, None, None)

        data = result["data"]
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["resolution"], "1080p")
        self.assertEqual(data[0]["media_id"], "upsampled-1")
        self.assertEqual(data[0]["source_media_id"], "native-1")
        # The 720p original is still returned and still downloadable.
        self.assertEqual(data[1]["resolution"], "720p")
        self.assertEqual(data[1]["media_id"], "native-1")

    async def test_default_video_generation_stays_native_and_never_upsamples(self):
        bridge = RecordingBridge([])
        never = AsyncMock(side_effect=AssertionError("upsample ran without being asked"))

        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=bridge)),
            patch("flow_engine.generators.t2v.generate_video", AsyncMock(return_value=["native-1"])),
            patch("flow_engine.generators.common.poll_status", AsyncMock(return_value=True)),
            patch("flow_engine.generators.common.download_video", _download),
            patch.object(generation, "publish", _publish),
            patch.object(generation, "append_to_history", _append_history),
            patch.object(generation, "upsample_video", never),
        ):
            result = await generation.openai_generate_video(
                VideoGenerationRequest(prompt="ocean waves"), None, None
            )

        self.assertEqual([item["resolution"] for item in result["data"]], ["720p"])
        self.assertEqual(never.await_count, 0)

    async def test_a_failed_upsample_still_delivers_the_720p_video_with_a_note(self):
        bridge = RecordingBridge([])
        failing = AsyncMock(side_effect=ValueError("upsampler is unavailable"))

        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=bridge)),
            patch("flow_engine.generators.t2v.generate_video", AsyncMock(return_value=["native-1"])),
            patch("flow_engine.generators.common.poll_status", AsyncMock(return_value=True)),
            patch("flow_engine.generators.common.download_video", _download),
            patch.object(generation, "publish", _publish),
            patch.object(generation, "append_to_history", _append_history),
            patch.object(generation, "upsample_video", failing),
        ):
            result = await generation.openai_generate_video(
                VideoGenerationRequest(prompt="ocean waves", resolution="1080p"), None, None
            )

        self.assertEqual([item["resolution"] for item in result["data"]], ["720p"])
        self.assertIn("could not be upsampled", result["note"])
        self.assertIn("upsampler is unavailable", result["note"])

    async def test_4k_is_refused_when_credits_cannot_cover_it(self):
        class BrokeBridge(RecordingBridge):
            async def api_request(self, path, body=None, captcha_action=None, method="POST"):
                if path == "/v1/credits":
                    return {"data": {"credits": 10}}
                return await super().api_request(path, body, captcha_action, method)

        bridge = BrokeBridge([])
        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=bridge)),
            patch.object(generation, "resolve_media_reference", AsyncMock(return_value="source-1")),
        ):
            with self.assertRaises(HTTPException) as exc:
                await generation.openai_upsample_video(
                    VideoUpsampleRequest(media_id="source-1", resolution="4k"), None, None
                )

        self.assertEqual(exc.exception.status_code, 402)
        self.assertEqual(bridge.calls, [])


class UpsampleToolSurfaceTests(unittest.TestCase):
    def test_both_mcp_transports_expose_the_same_upsample_tool(self):
        from flow_server.mcp.tools import get_mcp_tools_list
        from flow_server.mcp_server import handle_tools_list

        sse = {tool["name"]: tool for tool in get_mcp_tools_list()}
        stdio = {tool["name"]: tool for tool in handle_tools_list(1)["result"]["tools"]}

        for surface in (sse, stdio):
            self.assertIn("upsample_flow_video", surface)
            tool = surface["upsample_flow_video"]
            self.assertEqual(
                tool["inputSchema"]["properties"]["resolution"]["enum"], ["1080p", "4k"]
            )
            self.assertEqual(tool["inputSchema"]["required"], ["media_id"])

            video = surface["generate_flow_video"]["inputSchema"]["properties"]
            self.assertEqual(video["resolution"]["enum"], ["720p", "1080p", "4k"])
            self.assertEqual(video["resolution"]["default"], "720p")


class UpsampleConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_key_and_resolution_enum_come_from_configuration(self):
        # The Flow upsample API is undocumented, so a wire-format change must be
        # fixable from configuration (VIDEO_UPSAMPLER_*_MODEL /
        # VIDEO_UPSAMPLE_ENUM_*) rather than a code edit.
        bridge = RecordingBridge([_ok(["upsampled-1"])])

        with (
            patch.dict(upsample_mod.VIDEO_UPSAMPLE_MODELS, {"1080p": "custom_upsampler"}),
            patch.dict(upsample_mod.VIDEO_UPSAMPLE_RESOLUTIONS, {"1080p": "CUSTOM_ENUM"}),
        ):
            await upsample_video(bridge, "source", "VIDEO_ASPECT_RATIO_LANDSCAPE", "p")

        item = bridge.calls[0]["body"]["requests"][0]
        self.assertEqual(item["videoModelKey"], "custom_upsampler")
        self.assertEqual(item["resolution"], "CUSTOM_ENUM")


if __name__ == "__main__":
    unittest.main()
