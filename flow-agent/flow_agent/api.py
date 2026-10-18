"""FastAPI application: a native Flow API and an OpenAI-compatible one.

Two surfaces over one engine. The native routes speak the engine's own
vocabulary — aspect ratios, durations, quality keys — and the OpenAI routes
translate the shape an OpenAI client already sends, so an existing SDK or a chat
UI works without knowing anything about Flow.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .engine import EngineError, FlowEngine, clean_model

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
    "512x512": "1:1",
    "256x256": "1:1",
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
    model: str = "beluga"
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    response_format: str = Field(
        default="url",
        description=(
            "Response format. 'url' is the OpenAI default and what an unmodified "
            "SDK expects: JSON carrying a fetchable link. 'image' streams the raw "
            "bytes back for inline preview or download in a browser; 'b64_json' "
            "inlines the bytes in JSON."
        ),
    )
    # Flow-specific, ignored by OpenAI clients.
    all_accounts: bool = False
    cookies: Optional[str] = None

    @field_validator("model", mode="before")
    @classmethod
    def sanitize_model(cls, v: Any) -> str:
        # Swagger UI fills an untouched field with the literal text `string`.
        return clean_model(v)


class FlowImageRequest(BaseModel):
    prompt: str
    aspect: str = "1:1"
    count: int = Field(default=1, ge=1, le=4)
    model: str = "beluga"
    all_accounts: bool = False
    cookies: Optional[str] = None

    @field_validator("model", mode="before")
    @classmethod
    def sanitize_model(cls, v: Any) -> str:
        # Swagger UI fills an untouched field with the literal text `string`.
        return clean_model(v)


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


#: The characters a `host[:port]` may be built from: a DNS name, an IPv4 literal,
#: a bracketed IPv6 literal, or an underscore in an internal hostname. `@`, `\`
#: and `%` are absent on purpose - a userinfo `@` makes the host read as a
#: credential, and a browser normalises `\` to `/`.
_AUTHORITY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:[]_"
)


def _base_url(request: Request) -> str:
    """The scheme and authority to build a URL for this request from.

    Taken from the request's own base URL rather than a hardcoded host, so the
    answer is right whether this is reached on localhost, a LAN address or behind
    a proxy. `X-Forwarded-Proto` and `X-Forwarded-Host` are what a reverse proxy
    rewrites, so they are honoured - but they are also caller-supplied strings on
    their way into a response body, and a URL in a response is followed by
    whoever asked for it. A forwarded pair is therefore used only when it
    assembles into a bare `scheme://host[:port]`: no path, no query, no
    credentials. Anything else falls back to this framework's own base URL.
    """
    proto = request.headers.get("x-forwarded-proto", "").strip().lower()
    if proto not in ("http", "https"):
        # A proxy that forwards the host but not the scheme is the ordinary
        # case rather than an error, so the scheme falls back on its own instead
        # of discarding a host that is perfectly usable.
        proto = request.url.scheme

    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip()
    forwarded = f"{proto}://{host}" if host else ""

    try:
        parts = urlsplit(forwarded)
    except ValueError:
        # A malformed authority (an unclosed IPv6 bracket, say) is not usable.
        return str(request.base_url).rstrip("/")

    if (
        parts.scheme in ("http", "https")
        and parts.netloc
        and set(parts.netloc) <= _AUTHORITY_CHARS
        and not parts.path
        and not parts.query
        and not parts.fragment
    ):
        return f"{parts.scheme}://{parts.netloc}"
    return str(request.base_url).rstrip("/")


def _media_url(request: Request, path: str) -> str:
    """The URL a client can fetch one generated file from."""
    return f"{_base_url(request)}/api/v1/media/{Path(path).name}"


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


@app.post(
    "/v1/images/generations",
    responses={
        200: {
            "content": {
                "application/json": {},
                "image/png": {},
                "image/jpeg": {},
            },
            "description": "OpenAI-shaped JSON, or the image file itself for inline preview",
        }
    },
)
@app.post(
    "/api/v1/images/generations",
    responses={
        200: {
            "content": {
                "application/json": {},
                "image/png": {},
                "image/jpeg": {},
            },
            "description": "OpenAI-shaped JSON, or the image file itself for inline preview",
        }
    },
)
async def openai_images(payload: ImageGenerationRequest, request: Request):
    """Generate an image from an OpenAI images payload.

    `response_format` decides how the bytes come back:

    - `image` (also `file`, `binary`, `raw`, `preview`): the image file itself,
      streamed, so Swagger UI or a plain browser can show or save it at once.
    - `url` (the default): OpenAI's own shape, JSON carrying a download URL a
      client on another machine can fetch.
    - `b64_json`: the bytes inlined as a base64 string.

    The declared types list PNG as well as JPEG because the type of a streamed
    file is read from the file itself, so whichever one the engine wrote is the
    one the client is told.
    """
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

    files = result.get("files") or []
    if not files:
        raise HTTPException(status_code=502, detail=_no_image_detail(result))

    fmt = (payload.response_format or "").strip().lower()

    # Hand the bytes back untouched: this is the branch a browser and Swagger UI
    # use to show the image and its download button.
    if fmt in ("image", "file", "binary", "raw", "preview"):
        first_file = files[0].get("path")
        if first_file:
            img_path = Path(first_file)
            if img_path.is_file():
                # Read the type off the file, the same rule the media route
                # above uses. A hardcoded `image/jpeg` mislabels a PNG, and a
                # client that trusts the label saves the file instead of
                # showing it - the one thing this branch exists to do.
                media_type = mimetypes.guess_type(img_path.name)[0] or "application/octet-stream"
                return FileResponse(
                    img_path,
                    media_type=media_type,
                    filename=img_path.name,
                )

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


@app.post("/v1/generate/image")
@app.post("/v1/image")
@app.post("/api/v1/image")
async def flow_image(payload: FlowImageRequest, request: Request):
    """Generate an image, speaking the engine's own vocabulary.

    Each entry in `files` is also given a `url` a client can fetch directly, so
    a caller on a different machine from this process does not have to be told
    where the file was written.
    """
    try:
        result = await engine.generate_image(
            prompt=payload.prompt,
            aspect=payload.aspect,
            count=payload.count,
            model=payload.model,
            all_accounts=payload.all_accounts,
            cookies=payload.cookies,
        )
        # A caller that is not this process needs a URL, not a path.
        if isinstance(result, dict) and "files" in result and isinstance(result["files"], list):
            for f in result["files"]:
                if isinstance(f, dict) and "path" in f:
                    f["url"] = _media_url(request, f["path"])
        return result
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/generate/video")
@app.post("/v1/video")
@app.post("/api/v1/video")
async def flow_video(payload: FlowVideoRequest, request: Request):
    """Generate a video, speaking the engine's own vocabulary."""
    try:
        result = await engine.generate_video(
            prompt=payload.prompt,
            aspect=payload.aspect,
            duration=payload.duration,
            quality=payload.quality,
            count=payload.count,
            start_image=payload.start_image,
            all_accounts=payload.all_accounts,
            cookies=payload.cookies,
        )
        if isinstance(result, dict) and "files" in result and isinstance(result["files"], list):
            for f in result["files"]:
                if isinstance(f, dict) and "path" in f:
                    f["url"] = _media_url(request, f["path"])
        return result
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/balance")
@app.get("/api/v1/balance")
async def flow_balance(refresh: bool = False):
    """Credits per account, cached by default and probed with `?refresh=true`."""
    try:
        return await engine.get_balance(refresh=refresh)
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/stats")
@app.get("/api/v1/stats")
async def flow_stats():
    """Generation history and engine totals, read from the database."""
    try:
        return await engine.get_stats()
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/projects")
@app.get("/api/v1/projects")
async def flow_projects(cookies: Optional[str] = None):
    try:
        return {"projects": await engine.get_projects(cookies=cookies)}
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/media/{filename:path}")
@app.get("/api/v1/media/{filename:path}")
async def flow_media(filename: str):
    """Serve a generated media file, confined to a known output directory.

    Candidates are tried in order and each must resolve *inside* the directory
    it was found in. A string prefix is not that test: `/out` is a prefix of
    `/out-evil`, so a sibling directory that merely shares a prefix would be
    served. `Path.is_relative_to` compares path parts, so it cannot be fooled.

    `engine.root` is read defensively. It is optional on the engine contract,
    and a missing attribute must be a 404 here, not a 500.

    There is deliberately no `__file__`-relative guess at an output directory:
    the package's install layout has nothing to do with where the engine writes,
    so such a candidate either duplicates a directory already in the list or
    serves files from somewhere the engine never put them.

    The content type comes from `mimetypes` rather than a hand-kept extension
    table, because the preview page exists to render images inline - serving a
    PNG as `application/octet-stream` makes the browser download it instead.
    """
    search_dirs: list[Path] = [MEDIA_DIR]
    engine_root = getattr(engine, "root", None)
    if engine_root is not None:
        search_dirs.append(Path(engine_root) / "output")

    for sdir in search_dirs:
        base = Path(sdir).resolve()
        try:
            cand = (base / filename).resolve()
        except (OSError, ValueError):
            continue
        if not cand.is_file() or not cand.is_relative_to(base):
            continue
        media_type = mimetypes.guess_type(cand.name)[0] or "application/octet-stream"
        return FileResponse(cand, media_type=media_type, filename=cand.name)
    raise HTTPException(status_code=404, detail="no such media file")


@app.get("/health")
async def health():
    return {"status": "ok", "engine": engine.available(), "binary": str(engine.binary)}


# --------------------------------------------------------------------------- #
# web preview dashboard
# --------------------------------------------------------------------------- #

PREVIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Flow Agent - OpenAI image generation preview</title>
    <style>
        :root {
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --accent: #e11d48;
            --accent-hover: #be123c;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --border: #334155;
            --success: #22c55e;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 2rem 1rem;
        }
        .container {
            width: 100%;
            max-width: 840px;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 2rem;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
        }
        header {
            text-align: center;
            margin-bottom: 2rem;
        }
        h1 {
            font-size: 1.8rem;
            margin-bottom: 0.5rem;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
        }
        .badge {
            display: inline-block;
            background: rgba(225, 29, 72, 0.2);
            color: var(--accent);
            padding: 0.2rem 0.6rem;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 600;
        }
        p.subtitle {
            color: var(--text-secondary);
            font-size: 0.95rem;
        }
        .form-group {
            margin-bottom: 1.25rem;
        }
        label {
            display: block;
            margin-bottom: 0.5rem;
            font-weight: 600;
            font-size: 0.9rem;
            color: var(--text-primary);
        }
        textarea, select {
            width: 100%;
            padding: 0.85rem;
            border-radius: 8px;
            background: #0b1120;
            border: 1px solid var(--border);
            color: #fff;
            font-size: 1rem;
            resize: vertical;
        }
        textarea:focus, select:focus {
            outline: none;
            border-color: var(--accent);
        }
        .row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
        }
        button.btn-generate {
            width: 100%;
            padding: 1rem;
            background: var(--accent);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 1.05rem;
            font-weight: 700;
            cursor: pointer;
            transition: background 0.2s, transform 0.1s;
            margin-top: 0.5rem;
        }
        button.btn-generate:hover {
            background: var(--accent-hover);
        }
        button.btn-generate:disabled {
            background: #475569;
            cursor: not-allowed;
        }
        .result-section {
            margin-top: 2rem;
            border-top: 1px solid var(--border);
            padding-top: 2rem;
            display: none;
        }
        .status-box {
            padding: 0.85rem;
            border-radius: 8px;
            margin-bottom: 1rem;
            font-size: 0.95rem;
            background: rgba(59, 130, 246, 0.1);
            border: 1px solid rgba(59, 130, 246, 0.3);
            color: #60a5fa;
            text-align: center;
        }
        .image-container {
            text-align: center;
            background: #0b1120;
            border-radius: 12px;
            padding: 1rem;
            border: 1px solid var(--border);
        }
        .image-container img {
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5);
        }
        .action-bar {
            margin-top: 1rem;
            display: flex;
            gap: 1rem;
            justify-content: center;
        }
        .btn-action {
            padding: 0.6rem 1.2rem;
            border-radius: 6px;
            border: 1px solid var(--border);
            background: var(--bg-secondary);
            color: var(--text-primary);
            text-decoration: none;
            font-weight: 600;
            font-size: 0.9rem;
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            cursor: pointer;
        }
        .btn-action.download {
            background: var(--success);
            border-color: var(--success);
            color: #fff;
        }
        .links {
            margin-top: 2rem;
            text-align: center;
            font-size: 0.9rem;
            color: var(--text-secondary);
        }
        .links a {
            color: #38bdf8;
            text-decoration: none;
            margin: 0 0.5rem;
        }
        .links a:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>⚡ Flow Agent <span class="badge">OpenAI-compatible</span></h1>
            <p class="subtitle">Generate an image over <code>/v1/images/generations</code>, then preview or download it here.</p>
        </header>

        <form id="genForm">
            <div class="form-group">
                <label for="prompt">Prompt</label>
                <textarea id="prompt" rows="3" required placeholder="e.g. a serene Japanese zen garden in autumn, cinematic lighting, 8k">a glowing crystal lotus on calm water</textarea>
            </div>

            <div class="row">
                <div class="form-group">
                    <label for="size">Size / aspect</label>
                    <select id="size">
                        <option value="1024x1024" selected>1024x1024 (square 1:1)</option>
                        <option value="1792x1024">1792x1024 (wide 16:9)</option>
                        <option value="1024x1792">1024x1792 (tall 9:16)</option>
                        <option value="1365x1024">1365x1024 (landscape 4:3)</option>
                        <option value="1024x1365">1024x1365 (portrait 3:4)</option>
                    </select>
                </div>

                <div class="form-group">
                    <label for="format">Receive as</label>
                    <select id="format">
                        <option value="image" selected>Image bytes (preview/download at once)</option>
                        <option value="url">OpenAI JSON (URL link)</option>
                    </select>
                </div>
            </div>

            <button type="submit" id="btnSubmit" class="btn-generate">🎨 Generate image (OpenAI route)</button>
        </form>

        <div id="resultSection" class="result-section">
            <div id="statusBox" class="status-box">Generating...</div>
            <div id="imageBox" class="image-container" style="display: none;">
                <img id="resultImage" src="" alt="Generated image" />
                <div class="action-bar">
                    <a id="btnDownload" href="#" download="flow_image.jpg" class="btn-action download">📥 Download image</a>
                    <a id="btnNewTab" href="#" target="_blank" class="btn-action">🔍 Open the original</a>
                </div>
            </div>
        </div>

        <div class="links">
            <a href="/docs" target="_blank">📖 Swagger UI docs</a> |
            <a href="/redoc" target="_blank">📘 ReDoc</a> |
            <a href="/health" target="_blank">🩺 Server health (/health)</a>
        </div>
    </div>

    <script>
        const form = document.getElementById('genForm');
        const btnSubmit = document.getElementById('btnSubmit');
        const resultSection = document.getElementById('resultSection');
        const statusBox = document.getElementById('statusBox');
        const imageBox = document.getElementById('imageBox');
        const resultImage = document.getElementById('resultImage');
        const btnDownload = document.getElementById('btnDownload');
        const btnNewTab = document.getElementById('btnNewTab');

        let timerId = null;

        form.addEventListener('submit', async (e) => {
            e.preventDefault();

            const prompt = document.getElementById('prompt').value.trim();
            const size = document.getElementById('size').value;
            const format = document.getElementById('format').value;

            if (!prompt) return;

            btnSubmit.disabled = true;
            btnSubmit.innerText = '⏳ Generating... this takes a moment';
            resultSection.style.display = 'block';
            imageBox.style.display = 'none';

            let startTime = Date.now();
            statusBox.style.background = 'rgba(59, 130, 246, 0.1)';
            statusBox.style.color = '#60a5fa';
            statusBox.innerText = 'Asking Google Flow to generate... (0s elapsed)';

            timerId = setInterval(() => {
                const elapsed = Math.floor((Date.now() - startTime) / 1000);
                statusBox.innerText = `Generating on Google Flow and downloading... (${elapsed}s elapsed)`;
            }, 1000);

            try {
                const payload = {
                    prompt: prompt,
                    model: 'beluga',
                    n: 1,
                    size: size,
                    response_format: format
                };

                const res = await fetch('/v1/images/generations', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });

                clearInterval(timerId);
                const elapsedSec = ((Date.now() - startTime) / 1000).toFixed(1);

                if (!res.ok) {
                    const errText = await res.text();
                    statusBox.style.background = 'rgba(239, 68, 68, 0.1)';
                    statusBox.style.color = '#f87171';
                    statusBox.innerText = `[generation failed ${res.status}] ${errText}`;
                    btnSubmit.disabled = false;
                    btnSubmit.innerText = '🎨 Generate image (OpenAI route)';
                    return;
                }

                statusBox.style.background = 'rgba(34, 197, 94, 0.1)';
                statusBox.style.color = '#4ade80';
                statusBox.innerText = `✓ Done in ${elapsedSec}s`;

                let imgUrl = '';
                if (format === 'image') {
                    const blob = await res.blob();
                    imgUrl = URL.createObjectURL(blob);
                } else {
                    const data = await res.json();
                    if (data.data && data.data[0] && data.data[0].url) {
                        imgUrl = data.data[0].url;
                    }
                }

                if (imgUrl) {
                    resultImage.src = imgUrl;
                    btnDownload.href = imgUrl;
                    btnNewTab.href = imgUrl;
                    imageBox.style.display = 'block';
                }

            } catch (err) {
                clearInterval(timerId);
                statusBox.style.background = 'rgba(239, 68, 68, 0.1)';
                statusBox.style.color = '#f87171';
                statusBox.innerText = `[request failed] ${err.message}`;
            } finally {
                btnSubmit.disabled = false;
                btnSubmit.innerText = '🎨 Generate image (OpenAI route)';
            }
        });
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
@app.get("/preview", response_class=HTMLResponse)
async def preview_dashboard():
    """Generate over the OpenAI route from a browser and show the result at once."""
    return HTMLResponse(content=PREVIEW_HTML)
