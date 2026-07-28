#!/usr/bin/env python3
"""Chat completions endpoint (OpenAI-compatible)."""

import os
import uuid
import time
import json
import logging
import asyncio

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse

from flow_server.config import OUTPUT_DIR
from flow_server.media_types import ensure_correct_extension
from flow_server.models import ChatCompletionRequest
from flow_server.state import verify_api_key, get_active_bridge, publish, append_to_history

from flow_engine import DEFAULT_PROJECT
from flow_engine.generators.t2i import generate_image, download_image

# Setup logging (format configured centrally in flow_engine/__init__.py, imported above)
log = logging.getLogger("flow_engine.openai_api")

router = APIRouter()


async def stream_chat_completion(req: ChatCompletionRequest, content: str):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    timestamp = int(time.time())

    # Send assistant role chunk
    yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': timestamp, 'model': req.model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"
    await asyncio.sleep(0.02)

    # Send generated response chunk
    yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': timestamp, 'model': req.model, 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]})}\n\n"
    await asyncio.sleep(0.02)

    # Send stop signal
    yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': timestamp, 'model': req.model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
    yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(req: ChatCompletionRequest):
    """Expose image & video generation through standard chat endpoint for editors."""
    # Find last user prompt
    prompt = ""
    for msg in reversed(req.messages):
        if msg.role == "user":
            prompt = msg.content
            break

    if not prompt:
        raise HTTPException(status_code=400, detail="No user message found in the chat history.")

    # Check for short test/connection check prompts from IDEs (like 'hi', 'hello', 'test')
    is_test_prompt = len(prompt.strip()) < 5 or prompt.strip().lower() in ("hello", "test", "ping", "say hi", "hey", "hi there", "testing")

    if is_test_prompt:
        log.info(f"Test/Greeting prompt detected -> '{prompt}'. Returning mock response for verification.")
        markdown_response = "Hello! I am Flow-Agent. I am successfully connected and ready to generate images or videos for you."
    else:
        active_bridge = await get_active_bridge()
        project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

        # Detect video keywords in prompt
        is_video = any(kw in prompt.lower() for kw in ["video", "animate", "generate video", "make video", "mp4"])

        if is_video:
            # Import video generator on demand
            from flow_engine.generators.t2v import generate_video
            from flow_engine.generators.common import poll_status, download_video
            from flow_engine import ASPECTS

            log.info(f"Custom Chat Prompt: Video Generation -> '{prompt}'")
            aspect_key = ASPECTS.get("portrait", "VIDEO_ASPECT_RATIO_PORTRAIT")
            media_ids = await generate_video(active_bridge, prompt, aspect_key, project_id)
            if not media_ids:
                raise HTTPException(status_code=500, detail="Failed to initiate video generation.")

            media_id = media_ids[0]
            if not await poll_status(active_bridge, media_id, project_id):
                raise HTTPException(status_code=500, detail="Video generation failed during polling.")

            timestamp = int(time.time())
            filename = f"openai_chat_vid_{timestamp}_{uuid.uuid4().hex[:6]}_1.mp4"
            out_path = os.path.join(OUTPUT_DIR, filename)

            if not await download_video(active_bridge, media_id, out_path):
                raise HTTPException(status_code=500, detail="Failed to download video file.")

            out_path = ensure_correct_extension(out_path)
            filename = os.path.basename(out_path)
            download_url, r2_key = await publish(filename, out_path)
            await append_to_history(
                "video",
                download_url,
                prompt,
                media_id,
                r2_key,
                local_path=out_path,
                project_id=project_id,
            )
            markdown_response = f"**Video Generated successfully!**\n\n[Download / Play Video]({download_url})\n\n"
        else:
            log.info(f"Custom Chat Prompt: Image Generation -> '{prompt}'")
            results = await generate_image(active_bridge, prompt=prompt, aspect="square", project_id=project_id, count=1)
            if not results or not results[0].get("image_url"):
                raise HTTPException(status_code=500, detail="Flow failed to generate image.")

            url = results[0]["image_url"]
            timestamp = int(time.time())
            filename = f"openai_chat_img_{timestamp}_{uuid.uuid4().hex[:6]}_1.png"
            out_path = os.path.join(OUTPUT_DIR, filename)

            if not await download_image(active_bridge, url, out_path):
                raise HTTPException(status_code=500, detail="Failed to download image file.")

            out_path = ensure_correct_extension(out_path)
            filename = os.path.basename(out_path)
            download_url, r2_key = await publish(filename, out_path)
            await append_to_history(
                "image",
                download_url,
                prompt,
                results[0].get("media_id"),
                r2_key,
                local_path=out_path,
                project_id=project_id,
            )
            markdown_response = f" **Image Generated successfully!**\n\n![Generated Image]({download_url})\n\n"

    # Support streaming mode
    if req.stream:
        return StreamingResponse(
            stream_chat_completion(req, markdown_response),
            media_type="text/event-stream"
        )

    # Return standard non-streaming response
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": markdown_response
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": len(prompt) // 4,
            "completion_tokens": len(markdown_response) // 4,
            "total_tokens": (len(prompt) + len(markdown_response)) // 4
        }
    }
