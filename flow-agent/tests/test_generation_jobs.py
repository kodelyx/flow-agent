import base64
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from flow_server.idempotency import clear_idempotency_store_cache
from flow_server.jobs import clear_job_store_cache
from flow_server.models import ImageGenerationRequest, VideoGenerationRequest
from flow_server.routes import generation


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
WEBP = b"RIFF\x10\x00\x00\x00WEBP" + b"test-image"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"test-video"


class FakeBridge:
    def _select_client_for_cost(self, _cost):
        return None

    async def api_request(self, path, body=None, captcha_action=None, method="POST"):
        if path == "/v1/credits":
            return {"data": {"credits": 1000}}
        raise AssertionError(f"Unexpected bridge request: {method} {path}")


async def _publish(filename, _path):
    return f"http://test/download/{filename}", None


async def _append_history(*_args, **_kwargs):
    return None


class GenerationJobTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_image_generation_is_idempotent_and_uses_signature_extension(self):
        paid_calls = 0

        async def generate(*_args, **_kwargs):
            nonlocal paid_calls
            paid_calls += 1
            return [{"image_url": "https://flow/image", "media_id": "image-1"}]

        async def download(_bridge, _url, path):
            with open(path, "wb") as handle:
                handle.write(WEBP)
            return True

        request = ImageGenerationRequest(prompt="a lighthouse")
        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=FakeBridge())),
            patch.object(generation, "generate_image", generate),
            patch.object(generation, "download_image", download),
            patch.object(generation, "publish", _publish),
            patch.object(generation, "append_to_history", _append_history),
        ):
            first = await generation.openai_generate_image(request, None, "image-request-1")
            retry = await generation.openai_generate_image(request, None, "image-request-1")

            self.assertEqual(first, retry)
            self.assertEqual(paid_calls, 1)
            self.assertTrue(first["data"][0]["url"].endswith(".webp"))
            self.assertTrue(os.path.exists(os.path.join(self.tempdir.name, first["data"][0]["url"].rsplit("/", 1)[-1])))

            with self.assertRaises(HTTPException) as conflict:
                await generation.openai_generate_image(
                    ImageGenerationRequest(prompt="a different request"), None, "image-request-1"
                )
            self.assertEqual(conflict.exception.status_code, 409)

    async def test_image_to_video_returns_pollable_result_and_retry_does_not_resubmit(self):
        submit = AsyncMock(return_value=["video-media-1"])

        async def upload_image(_bridge, path, _project_id):
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), PNG)
            return "uploaded-start-image"

        async def download_video(_bridge, _media_id, path):
            with open(path, "wb") as handle:
                handle.write(MP4)
            return True

        request = VideoGenerationRequest(
            prompt="slow camera push",
            image_base64="data:image/png;base64," + base64.b64encode(PNG).decode("ascii"),
        )
        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=FakeBridge())),
            patch("flow_engine.generators.i2v.upload_image", upload_image),
            patch("flow_engine.generators.i2v.generate_video_i2v", submit),
            patch("flow_engine.generators.common.poll_status", AsyncMock(return_value=True)),
            patch("flow_engine.generators.common.download_video", download_video),
            patch.object(generation, "publish", _publish),
            patch.object(generation, "append_to_history", _append_history),
        ):
            first = await generation.openai_generate_video(request, None, "video-request-1")
            retry = await generation.openai_generate_video(request, None, "video-request-1")
            polled = await generation.get_video_generation(first["job_id"])

        self.assertEqual(first, retry)
        self.assertEqual(first, polled)
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["data"][0]["media_id"], "video-media-1")
        self.assertEqual(submit.await_count, 1)
        self.assertIn("uploaded-start-image", submit.await_args.args)

    async def test_idempotent_video_result_survives_store_restart(self):
        async def download_video(_bridge, _media_id, path):
            with open(path, "wb") as handle:
                handle.write(MP4)
            return True

        request = VideoGenerationRequest(prompt="ocean waves")
        paid_submit = AsyncMock(return_value=["video-media-2"])
        with (
            patch.object(generation, "get_active_bridge", AsyncMock(return_value=FakeBridge())),
            patch("flow_engine.generators.t2v.generate_video", paid_submit),
            patch("flow_engine.generators.common.poll_status", AsyncMock(return_value=True)),
            patch("flow_engine.generators.common.download_video", download_video),
            patch.object(generation, "publish", _publish),
            patch.object(generation, "append_to_history", _append_history),
        ):
            first = await generation.openai_generate_video(request, None, "durable-video-key")

        # Simulate a new backend process: only the durable JSON files remain.
        clear_idempotency_store_cache()
        clear_job_store_cache()
        should_not_run = AsyncMock(side_effect=AssertionError("paid generation was duplicated"))
        with patch("flow_engine.generators.t2v.generate_video", should_not_run):
            replay = await generation.openai_generate_video(request, None, "durable-video-key")
            polled = await generation.get_video_generation(first["job_id"])

        self.assertEqual(first, replay)
        self.assertEqual(first, polled)
        self.assertEqual(paid_submit.await_count, 1)
        self.assertEqual(should_not_run.await_count, 0)


if __name__ == "__main__":
    unittest.main()
