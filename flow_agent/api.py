"""FastAPI application: a native Flow API and an OpenAI-compatible one.

Two surfaces over one engine. The native routes speak the engine's own
vocabulary — aspect ratios, durations, quality keys — and the OpenAI routes
translate the shape an OpenAI client already sends, so an existing SDK or a chat
UI works without knowing anything about Flow.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .engine import EngineError, FlowEngine

app = FastAPI(
    title="flow-agent",
    version="1.0.0",
    description="Image and video generation on Google Flow.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = FlowEngine()


# OpenAI's image sizes, and the aspect each one means.
#
# The pairs are not invented here: OpenAI's three sizes are 1:1, 3:2 and 2:3, and
# the Flow ratios that match are square, landscape and portrait. The two extra
# entries are Flow's own 4:3 and 3:4, which have no OpenAI size and are reachable
# through the native route.
SIZE_TO_ASPECT = {
    "1024x1024": "1:1",
    "1792x1024": "16:9",
    "1024x1792": "9:16",
    "1365x1024": "4:3",
    "1024x1365": "3:4",
    # The aliases a caller may use instead of pixels.
    "square": "1:1",
    "landscape": "16:9",
    "portrait": "9:16",
}

MEDIA_DIR = engine.output_dir


# --------------------------------------------------------------------------- #
# request models
# --------------------------------------------------------------------------- #


class ImageGenerationRequest(BaseModel):
    """The OpenAI images payload, plus the two Flow extras.

    Extra fields are allowed rather than rejected: an OpenAI client sends keys
    this does not model, and failing the request over a field nobody needs would
    make the endpoint unusable from the SDKs it exists to serve.
    """

    model_config = ConfigDict(extra="allow")

    prompt: str
    model: str = "narwhal"
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    response_format: str = "url"
    # Flow-specific, ignored by OpenAI clients.
    all_accounts: bool = False
    cookies: Optional[str] = None


class FlowImageRequest(BaseModel):
    prompt: str
    aspect: str = "1:1"
    count: int = Field(default=1, ge=1, le=4)
    model: str = "narwhal"
    all_accounts: bool = False
    cookies: Optional[str] = None


class FlowVideoRequest(BaseModel):
    prompt: str
    aspect: str = "landscape"
    duration: str = "8s"
    quality: str = "720p"
    count: int = Field(default=1, ge=1, le=4)
    start_image: Optional[str] = None
    all_accounts: bool = False
    cookies: Optional[str] = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = ""


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "flow"
    messages: list[ChatMessage]
    stream: bool = False
    # Flow-specific.
    size: str = "1024x1024"
    all_accounts: bool = False
    cookies: Optional[str] = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _media_url(request: Request, path: str) -> str:
    """The URL a client can fetch one generated file from.

    Built from the request's own base URL rather than a hardcoded host, so the
    response is correct whether this is reached on localhost, a LAN address or
    behind a proxy.
    """
    return f"{str(request.base_url).rstrip('/')}/api/v1/media/{Path(path).name}"


def _no_image_detail(result: dict) -> str:
    """Why a run that exited zero produced nothing usable.

    The engine reports a refusal in the result rather than by failing, so this
    is where the two are told apart. The status and job id are named because
    they are what a caller needs to look the run up; `status` is `empty` for a
    refusal and `succeeded` for a job whose files could not be read.
    """
    return (
        "the engine produced no image "
        f"(status={result.get('status')!r}, job={result.get('job_id')!r}, "
        f"account={result.get('account_id')!r})"
    )


def _image_payload(request: Request, result: dict, response_format: str) -> dict:
    """Turn an engine result into the OpenAI images response."""
    files = result.get("files") or []
    data: list[dict] = []

    for item in files:
        path = item.get("path")
        if not path:
            continue
        if response_format == "b64_json":
            try:
                data.append(
                    {"b64_json": base64.b64encode(Path(path).read_bytes()).decode()}
                )
            except OSError:
                continue
        else:
            data.append({"url": _media_url(request, path)})

    return {"created": int(time.time()), "data": data, "flow": result}


def _prompt_from_messages(messages: list[ChatMessage]) -> str:
    """The text of the last user turn, which is what the engine generates from."""
    for message in reversed(messages):
        if message.role != "user":
            continue
        content = message.content
        if isinstance(content, str):
            return content
        # The newer OpenAI shape is a list of parts; take the text ones.
        if isinstance(content, list):
            parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            joined = " ".join(p for p in parts if p).strip()
            if joined:
                return joined
    return ""


def _completion_body(model: str, markdown: str) -> dict:
    return {
        "id": f"chatcmpl-flow-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": markdown},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _sse(body: dict) -> StreamingResponse:
    """Stream one completion as the two SSE frames an OpenAI client expects.

    The content is delivered in one delta rather than token by token: the engine
    produces an image, not prose, so there is nothing to stream progressively —
    but a client that asked for `stream: true` still needs the framing.
    """

    async def frames():
        head = {
            "id": body["id"],
            "object": "chat.completion.chunk",
            "created": body["created"],
            "model": body["model"],
            "choices": [
                {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
            ],
        }
        yield f"data: {json.dumps(head)}\n\n"

        chunk = {
            **head,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": body["choices"][0]["message"]["content"]},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n"

        tail = {
            **head,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(tail)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# OpenAI-compatible
# --------------------------------------------------------------------------- #


@app.post("/v1/images/generations")
async def openai_images(payload: ImageGenerationRequest, request: Request):
    """Generate an image from an OpenAI images payload."""
    aspect = SIZE_TO_ASPECT.get(payload.size.strip().lower())
    if aspect is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported size {payload.size!r}; use one of "
                f"{', '.join(sorted(SIZE_TO_ASPECT))}"
            ),
        )

    try:
        result = await engine.generate_image(
            prompt=payload.prompt,
            aspect=aspect,
            count=payload.n,
            model=payload.model,
            all_accounts=payload.all_accounts,
            cookies=payload.cookies,
        )
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    payload_out = _image_payload(request, result, payload.response_format)
    # A run that produced nothing is not a success. Returning 200 with an empty
    # `data` would tell an OpenAI client the request worked and there simply
    # were no images, which is the opposite of what happened.
    if not payload_out["data"]:
        raise HTTPException(status_code=502, detail=_no_image_detail(result))

    return payload_out


@app.post("/v1/chat/completions")
async def openai_chat(payload: ChatCompletionRequest, request: Request):
    """Generate from the last user turn and answer with a Markdown link."""
    prompt = _prompt_from_messages(payload.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="no user message with text to generate from")

    aspect = SIZE_TO_ASPECT.get(payload.size.strip().lower(), "1:1")

    try:
        result = await engine.generate_image(
            prompt=prompt,
            aspect=aspect,
            all_accounts=payload.all_accounts,
            cookies=payload.cookies,
        )
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    files = result.get("files") or []
    if not files:
        markdown = _no_image_detail(result)
    else:
        links = "\n".join(
            f"![generated image]({_media_url(request, f['path'])})" for f in files if f.get("path")
        )
        markdown = links or "The engine reported files but named no paths."

    body = _completion_body(payload.model, markdown)
    return _sse(body) if payload.stream else body


# --------------------------------------------------------------------------- #
# native
# --------------------------------------------------------------------------- #


@app.post("/api/v1/image")
async def flow_image(payload: FlowImageRequest):
    """Generate an image, speaking the engine's own vocabulary."""
    try:
        return await engine.generate_image(
            prompt=payload.prompt,
            aspect=payload.aspect,
            count=payload.count,
            model=payload.model,
            all_accounts=payload.all_accounts,
            cookies=payload.cookies,
        )
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/video")
async def flow_video(payload: FlowVideoRequest):
    """Generate a video, speaking the engine's own vocabulary."""
    try:
        return await engine.generate_video(
            prompt=payload.prompt,
            aspect=payload.aspect,
            duration=payload.duration,
            quality=payload.quality,
            count=payload.count,
            start_image=payload.start_image,
            all_accounts=payload.all_accounts,
            cookies=payload.cookies,
        )
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/v1/balance")
async def flow_balance(refresh: bool = False):
    """Credits per account, cached by default and probed with `?refresh=true`."""
    try:
        return await engine.get_balance(refresh=refresh)
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/v1/stats")
async def flow_stats():
    """Generation history and engine totals, read from the database."""
    try:
        return await engine.get_stats()
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/v1/projects")
async def flow_projects(cookies: Optional[str] = None):
    try:
        return {"projects": await engine.get_projects(cookies=cookies)}
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/v1/media/{filename}")
async def flow_media(filename: str):
    """Serve one generated file.

    The name is resolved against the output directory and rejected if it escapes
    it: this route takes a path from a URL, and `..` in one is the whole attack.
    """
    candidate = (MEDIA_DIR / filename).resolve()
    if candidate.parent != MEDIA_DIR.resolve() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="no such media file")
    return FileResponse(candidate)


@app.get("/health")
async def health():
    return {"status": "ok", "engine": engine.available(), "binary": str(engine.binary)}
