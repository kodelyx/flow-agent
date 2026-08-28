#!/usr/bin/env python3
"""Unified Flow CLI.

The single ``flow`` executable starts the backend by default and exposes MCP,
image, video, edit, upload, and developer commands as subcommands.  All media
commands use the one backend bridge on port 8001; they never create their own
``ExtensionBridge``.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

SHUTDOWN_TIMEOUT = int(os.environ.get("SHUTDOWN_TIMEOUT", "5"))
EXIT_BACKEND_UNAVAILABLE = 3
EXIT_NOT_READY = 4
EXIT_API_ERROR = 5
EXIT_OUTPUT_ERROR = 6


class CliError(Exception):
    """An expected CLI failure with a stable process exit code."""

    def __init__(self, message, exit_code=1, retryable=False):
        super().__init__(message)
        self.exit_code = exit_code
        self.retryable = retryable


def _api_base() -> str:
    host = os.environ.get("OPENAI_API_HOST", "127.0.0.1")
    port = os.environ.get("OPENAI_API_PORT", "8001")
    return f"http://{host}:{port}"


def _fail(message, exit_code=1):
    print(f"flow: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def _error_detail(raw):
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw).strip()
    if isinstance(value, dict):
        detail = value.get("detail", value.get("error", value))
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
        return str(detail)
    return str(value)


def _request_json(path, *, method="GET", payload=None, headers=None, timeout=30):
    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        f"{_api_base()}{path}", data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response), getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        detail = _error_detail(error.read().decode("utf-8", errors="replace"))
        raise CliError(
            f"backend request {method} {path} failed (HTTP {error.code}): {detail}",
            EXIT_API_ERROR,
            retryable=error.code >= 500,
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CliError(
            f"could not reach the backend at {_api_base()}: {error}",
            EXIT_BACKEND_UNAVAILABLE,
            retryable=True,
        ) from error
    except (json.JSONDecodeError, ValueError) as error:
        raise CliError(
            f"backend at {_api_base()} returned invalid JSON: {error}",
            EXIT_API_ERROR,
        ) from error


def _probe_backend(timeout=2):
    """Return health JSON for a Flow backend, otherwise ``None``."""

    try:
        data, status = _request_json("/health", timeout=timeout)
    except CliError:
        return None
    if status != 200 or not isinstance(data, dict):
        return None
    # Do not mistake an unrelated service on port 8001 for Flow.  Older Flow
    # versions may only return status/connected while their bridge starts.
    flow_statuses = {"starting", "healthy", "unauthorized_or_disconnected"}
    if data.get("status") not in flow_statuses:
        return None
    return data


def _ensure_backend():
    """Reuse the port-8001 backend, starting exactly one only when absent."""

    health = _probe_backend()
    if health is not None:
        return health

    import subprocess

    print(f"[flow] No Flow backend found at {_api_base()}; starting one...")
    try:
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(os.path.abspath(__file__))
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as error:
        _fail(
            f"could not start the backend: {error}. Start it manually with `flow`.",
            EXIT_BACKEND_UNAVAILABLE,
        )

    startup_timeout = float(os.environ.get("FLOW_BACKEND_START_TIMEOUT", "15"))
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        time.sleep(0.25)
        health = _probe_backend(timeout=1)
        if health is not None:
            print(f"[flow] Backend started at {_api_base()}.")
            return health

    _fail(
        f"backend did not become reachable at {_api_base()} within "
        f"{startup_timeout:g}s. Run `flow` in another terminal and inspect its logs.",
        EXIT_BACKEND_UNAVAILABLE,
    )


def _wait_for_generation_ready(timeout=None):
    """Wait for both the extension socket and captured Flow key."""

    health = _ensure_backend()
    if health.get("extension_connected") is True and health.get("has_flow_key") is True:
        return health

    if timeout is None:
        timeout = float(os.environ.get("FLOW_READY_TIMEOUT", "30"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        health = _probe_backend(timeout=2) or {}
        if health.get("extension_connected") is True and health.get("has_flow_key") is True:
            return health

    extension = bool(health.get("extension_connected"))
    flow_key = bool(health.get("has_flow_key"))
    _fail(
        "backend is running but Google Flow is not ready "
        f"(extension_connected={extension}, has_flow_key={flow_key}). "
        "Open Google Flow in Chrome, enable the Flow Agent extension, refresh the tab, "
        "and retry.",
        EXIT_NOT_READY,
    )


def _default_output_path(filename):
    output_dir = os.environ.get("FLOW_OUTPUT_DIR")
    if not output_dir:
        # Keep the CLI, API, media-id persistence, and packaged executable on
        # one definition of "beside the flow binary".
        from flow_engine.config import OUTPUT_DIR

        output_dir = OUTPUT_DIR
    return os.path.abspath(os.path.expanduser(os.path.join(output_dir, filename)))


def _requested_output_path(path, default_filename):
    if path is None:
        return _default_output_path(default_filename), False
    # An explicit absolute path remains that exact path. Relative explicit paths
    # are resolved once against the caller's cwd and then treated identically.
    return os.path.abspath(os.path.expanduser(path)), True


def _numbered_output(path, index):
    """Keep the first result at the exact requested path; suffix later results."""

    if index == 0:
        return path
    stem, extension = os.path.splitext(path)
    return f"{stem}_{index + 1}{extension}"


def _sniff_media_type(data, filename=""):
    try:
        from flow_server.media_types import sniff_media_type

        # A filename is deliberately not supplied: local uploads must be
        # classified from their bytes, never from a misleading suffix.
        mime = sniff_media_type(data)
        if mime.startswith(("image/", "video/")):
            return mime
        _fail(
            f"cannot determine media type from the file signature: {filename}",
            EXIT_OUTPUT_ERROR,
        )
    except (ImportError, TypeError):
        pass
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/quicktime" if data[8:12] == b"qt  " else "video/mp4"
    _fail(
        f"cannot determine media type from the file signature: {filename}",
        EXIT_OUTPUT_ERROR,
    )


def _file_data_uri(path):
    try:
        with open(path, "rb") as media_file:
            data = media_file.read()
    except OSError as error:
        _fail(f"could not read media file {path}: {error}", EXIT_OUTPUT_ERROR)
    mime = _sniff_media_type(data, path)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _download_media(url, output_path, *, preserve_explicit=True):
    output_path = os.path.abspath(os.path.expanduser(output_path))
    temporary_path = f"{output_path}.flow-download-{uuid.uuid4().hex}"
    declared_mime = ""
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as response, open(
            temporary_path, "wb"
        ) as output_file:
            headers = getattr(response, "headers", None)
            if headers is not None:
                declared_mime = headers.get("Content-Type", "")
            output_file.write(response.read())
            output_file.flush()
            os.fsync(output_file.fileno())

        from flow_server.media_types import (
            convert_image_atomic,
            extension_for_mime,
            requested_image_mime,
            sniff_media_type,
        )

        # Pass an open stream rather than the path so an unknown signature
        # cannot silently fall back to the requested filename extension.
        with open(temporary_path, "rb") as downloaded_file:
            actual_mime = sniff_media_type(downloaded_file)
        if not actual_mime.startswith(("image/", "video/")):
            mime_hint = f"; server declared {declared_mime}" if declared_mime else ""
            raise ValueError(
                f"downloaded data from {url} is not recognized image/video media "
                f"(signature detected {actual_mime}{mime_hint})"
            )

        requested_mime = requested_image_mime(output_path) if preserve_explicit else None
        if (
            requested_mime
            and actual_mime.startswith("image/")
            and requested_mime != actual_mime
        ):
            # The requested suffix is a format contract. Decode and encode the
            # image instead of ever exposing mismatched bytes at output_path.
            convert_image_atomic(temporary_path, output_path, requested_mime)
            return output_path

        final_path = output_path
        if not preserve_explicit:
            actual_extension = extension_for_mime(actual_mime)
            current_extension = os.path.splitext(output_path)[1].lower()
            if actual_extension and current_extension != actual_extension:
                stem = os.path.splitext(output_path)[0]
                final_path = (
                    f"{stem}{actual_extension}"
                    if current_extension
                    else output_path + actual_extension
                )
                if os.path.exists(final_path):
                    raise FileExistsError(
                        f"refusing to replace existing media file: {final_path}"
                    )

        os.replace(temporary_path, final_path)
        return final_path
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as error:
        _fail(
            f"generated media could not be saved to {output_path}: {error}. "
            "The backend result remains available from its returned URL.",
            EXIT_OUTPUT_ERROR,
        )
    finally:
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass


def _post_generation(path, payload, idempotency_key, timeout=300):
    headers = {"Idempotency-Key": idempotency_key}
    last_error = None
    for attempt in range(2):
        try:
            result, _ = _request_json(
                path, method="POST", payload=payload, headers=headers, timeout=timeout
            )
            return result
        except CliError as error:
            last_error = error
            if not error.retryable or attempt == 1:
                break
            print(
                f"[flow] Request interrupted; retrying safely with idempotency key "
                f"{idempotency_key}...",
                file=sys.stderr,
            )
            time.sleep(0.5)
    message = str(last_error)
    if last_error.retryable or "processing" in message.lower():
        message += (
            f". Retry with `--idempotency-key {idempotency_key}`; "
            "the backend will reuse the original paid generation"
        )
    _fail(message, last_error.exit_code)


def _poll_video_job(result, idempotency_key):
    job_id = result.get("job_id") if isinstance(result, dict) else None
    status = str(result.get("status", "")).lower() if isinstance(result, dict) else ""
    if not job_id or (result.get("data") and status not in {"queued", "pending", "processing"}):
        return result

    if status in {"succeeded", "completed", "done"}:
        return result
    print(f"Video job submitted: job_id={job_id}")
    timeout = float(os.environ.get("FLOW_VIDEO_POLL_TIMEOUT", "900"))
    interval = float(os.environ.get("FLOW_VIDEO_POLL_INTERVAL", "1"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            result, _ = _request_json(f"/v1/videos/generations/{job_id}", timeout=30)
        except CliError as error:
            if error.retryable:
                continue
            _fail(str(error), error.exit_code)
        status = str(result.get("status", "")).lower()
        if status in {"succeeded", "completed", "done"}:
            return result
        if status in {"failed", "error", "cancelled", "canceled"}:
            detail = result.get("error") or result.get("detail") or "unknown backend error"
            _fail(f"video job {job_id} failed: {detail}", EXIT_API_ERROR)

    _fail(
        f"video job {job_id} is still running after {timeout:g}s. "
        f"Retry with `--idempotency-key {idempotency_key}` to resume without another charge.",
        EXIT_API_ERROR,
    )


def _save_outputs(result, output_path, media_label, *, explicit_output):
    outputs = result.get("data", []) if isinstance(result, dict) else []
    if not outputs:
        _fail(f"Flow returned no {media_label} output URLs: {result}", EXIT_API_ERROR)

    saved = []
    for index, item in enumerate(outputs):
        media_url = item.get("url") if isinstance(item, dict) else None
        if not media_url:
            _fail(f"Flow returned a {media_label} result without a URL: {item}", EXIT_API_ERROR)
        destination = _numbered_output(output_path, index)
        destination = _download_media(
            media_url, destination, preserve_explicit=explicit_output
        )
        from flow_server.media_types import format_name_for_mime, sniff_media_type

        with open(destination, "rb") as saved_file:
            actual_mime = sniff_media_type(saved_file)
        print(
            f"Done! {os.path.abspath(destination)} "
            f"(format: {format_name_for_mime(actual_mime)})"
        )
        if item.get("media_id"):
            print(f"media_id={item['media_id']}")
        if item.get("resolution"):
            print(f"resolution={item['resolution']}")
        saved.append(destination)
    if result.get("note"):
        print(result["note"])
    return saved


def _forward(module_main, argv):
    _ensure_backend()
    saved = sys.argv
    sys.argv = [saved[0]] + argv
    try:
        module_main()
    finally:
        sys.argv = saved


def run_backend(argv):
    parser = argparse.ArgumentParser(
        prog="flow",
        description="Start the Flow backend, MCP-over-SSE, and extension bridge.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("OPENAI_API_HOST", "127.0.0.1"),
        help="Bind address (use 0.0.0.0 to expose on the network).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("OPENAI_API_PORT", "8001")),
        help="Port (default: 8001).",
    )
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")
    args = parser.parse_args(argv)

    # Invoking `flow` a second time should attach to/reuse the healthy service,
    # not ask uvicorn to bind the same port and start another bridge lifespan.
    existing = _probe_backend()
    if existing is not None:
        print(f"Flow backend already running at {_api_base()}; reusing it.")
        return

    import uvicorn

    print(f"Flow starting on http://{args.host}:{args.port}")
    print("Waiting for the Chrome extension (open Google Flow in Chrome to connect).")
    uvicorn.run(
        "flow_server.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        access_log=False,
        timeout_graceful_shutdown=SHUTDOWN_TIMEOUT,
    )


def run_mcp(argv):
    parser = argparse.ArgumentParser(prog="flow mcp", description="Run Flow's MCP stdio transport.")
    parser.parse_args(argv)
    from flow_server.mcp_server import main as mcp_main

    mcp_main()


def cmd_image(argv):
    parser = argparse.ArgumentParser(prog="flow image", description="Generate images through the Flow backend.")
    parser.add_argument("prompt", help="Text prompt for image")
    parser.add_argument("--output", "-o", default=None, help="Exact output file path")
    parser.add_argument(
        "--aspect",
        "-a",
        choices=["landscape", "4x3", "square", "3x4", "portrait"],
        default="portrait",
    )
    parser.add_argument("--count", "-c", type=int, choices=[1, 2, 3, 4], default=1)
    parser.add_argument("--ref", "-r", nargs="+", metavar="IMAGE", help="Reference path or media ID")
    parser.add_argument("--model", "-m", default="narwhal")
    parser.add_argument("--idempotency-key", help="Reuse a previous paid request safely")
    args = parser.parse_args(argv)

    _wait_for_generation_ready()
    output_path, explicit_output = _requested_output_path(args.output, "image.png")
    sizes = {
        "landscape": "1792x1024",
        "4x3": "1365x1024",
        "square": "1024x1024",
        "3x4": "1024x1365",
        "portrait": "1024x1792",
    }
    payload = {
        "prompt": args.prompt,
        "model": args.model,
        "n": args.count,
        "size": sizes[args.aspect],
        "response_format": "url",
    }

    media_ids = []
    local_refs = []
    for ref in args.ref or []:
        if os.path.isfile(ref):
            local_refs.append(ref)
        else:
            media_ids.append(ref)
    if len(local_refs) > 1:
        parser.error("flow image currently accepts at most one local reference file")
    if local_refs:
        payload["image_base64"] = _file_data_uri(local_refs[0])
    if media_ids:
        payload["ref_media_ids"] = media_ids[:10]

    idempotency_key = args.idempotency_key or uuid.uuid4().hex
    result = _post_generation(
        "/v1/images/generations", payload, idempotency_key, timeout=300
    )
    _save_outputs(result, output_path, "image", explicit_output=explicit_output)


def _upload_one(path):
    result, _ = _request_json(
        "/v1/upload",
        method="POST",
        payload={"image_base64": _file_data_uri(path)},
        timeout=300,
    )
    media_id = result.get("media_id") if isinstance(result, dict) else None
    if not media_id:
        _fail(f"backend upload returned no media_id for {path}: {result}", EXIT_API_ERROR)
    return result


def cmd_video(argv):
    parser = argparse.ArgumentParser(prog="flow video", description="Generate video through the shared Flow backend.")
    parser.add_argument("prompt", help="Text prompt for video")
    parser.add_argument("--output", "-o", default=None, help="Exact output file path")
    parser.add_argument("--aspect", "-a", choices=["portrait", "landscape"], default="portrait")
    parser.add_argument("--duration", "-d", type=int, choices=[4, 6, 8, 10], default=10)
    parser.add_argument("--count", "-c", type=int, choices=[1, 2, 3, 4], default=1)
    parser.add_argument("--edit", "-e", metavar="MEDIA_ID", help="Edit an existing video media ID")
    parser.add_argument("--start", "-s", metavar="IMAGE", help="Start image path or media ID")
    parser.add_argument("--end", metavar="IMAGE", help="End image path or media ID (use with --start)")
    parser.add_argument("--ref", "-r", nargs="+", metavar="IMAGE", help="Reference image paths or media IDs")
    parser.add_argument("--project-id", "-p", help="Deprecated; configure DEFAULT_PROJECT on the backend")
    parser.add_argument(
        "--resolution",
        "-R",
        choices=["720p", "1080p", "4k"],
        default="720p",
        help=(
            "Delivery resolution. Flow generates at 720p; 1080p (free) and 4k (paid, "
            "higher tier) add Flow's upsampler pass and the high-resolution file is "
            "written to --output."
        ),
    )
    parser.add_argument("--idempotency-key", help="Reuse a previous paid request safely")
    args = parser.parse_args(argv)

    if args.end and not args.start:
        parser.error("--end requires --start")
    modes = sum(bool(value) for value in (args.edit, args.start or args.end, args.ref))
    if modes > 1:
        parser.error("use only one of --edit, --start, or --ref")

    _wait_for_generation_ready()
    output_path, explicit_output = _requested_output_path(args.output, "video.mp4")
    payload = {
        "prompt": args.prompt,
        "aspect": args.aspect,
        "duration": args.duration,
        "n": args.count,
    }
    if args.resolution != "720p":
        payload["resolution"] = args.resolution
    if args.edit:
        payload.update({"start_media_id": args.edit, "is_video": True})
    elif args.start:
        if os.path.isfile(args.start):
            payload["image_base64"] = _file_data_uri(args.start)
        else:
            payload["start_media_id"] = args.start
        if args.end:
            if os.path.isfile(args.end):
                payload["end_media_id"] = _upload_one(args.end)["media_id"]
            else:
                payload["end_media_id"] = args.end
    elif args.ref:
        reference_ids = []
        for reference in args.ref:
            if os.path.isfile(reference):
                reference_ids.append(_upload_one(reference)["media_id"])
            else:
                reference_ids.append(reference)
        payload["ref_media_ids"] = reference_ids[:10]

    idempotency_key = args.idempotency_key or uuid.uuid4().hex
    result = _post_generation(
        "/v1/videos/generations", payload, idempotency_key, timeout=300
    )
    result = _poll_video_job(result, idempotency_key)
    _save_outputs(result, output_path, "video", explicit_output=explicit_output)


def cmd_edit(argv):
    parser = argparse.ArgumentParser(
        prog="flow edit",
        description="Edit one generated video through the shared Flow backend.",
    )
    parser.add_argument("prompt", help="Edit prompt")
    parser.add_argument("--media-id", "-m", required=True, help="Flow video media ID")
    parser.add_argument("--video-file", "-v", help="Deprecated; the backend uses --media-id")
    parser.add_argument("--total-seconds", "-t", type=float, help="Edit duration: 4, 6, 8, or 10")
    parser.add_argument("--duration", "-d", type=int, choices=[4, 6, 8, 10], default=10)
    parser.add_argument("--output", "-o", default=None, help="Exact output file path")
    parser.add_argument("--aspect", "-a", choices=["portrait", "landscape"], default="portrait")
    parser.add_argument("--merge", action="store_true", help="Deprecated; one backend edit is produced")
    parser.add_argument("--project-id", "-p", help="Deprecated; configure DEFAULT_PROJECT on the backend")
    parser.add_argument("--idempotency-key", help="Reuse a previous paid request safely")
    args = parser.parse_args(argv)

    duration = args.duration
    if args.total_seconds is not None:
        if args.total_seconds not in {4, 6, 8, 10}:
            parser.error(
                "the shared backend supports edit durations of 4, 6, 8, or 10 seconds; "
                "long segmented edits are not available"
            )
        duration = int(args.total_seconds)
    forwarded = [
        args.prompt,
        "--edit",
        args.media_id,
        "--duration",
        str(duration),
        "--aspect",
        args.aspect,
    ]
    if args.output:
        output = args.output
        if output.endswith(os.sep) or os.path.isdir(output):
            output = os.path.join(output, "edited_video.mp4")
        forwarded.extend(["--output", output])
    if args.idempotency_key:
        forwarded.extend(["--idempotency-key", args.idempotency_key])
    cmd_video(forwarded)


def cmd_upload(argv):
    parser = argparse.ArgumentParser(prog="flow upload", description="Upload media through the shared Flow backend.")
    parser.add_argument("path", help="Media file or directory of .mp4 videos")
    parser.add_argument("--batch", "-b", action="store_true", help="Upload all .mp4 files in a directory")
    parser.add_argument("--project-id", "-p", help="Deprecated; configure DEFAULT_PROJECT on the backend")
    args = parser.parse_args(argv)

    _wait_for_generation_ready()
    if args.batch or os.path.isdir(args.path):
        if not os.path.isdir(args.path):
            parser.error("--batch requires a directory path")
        paths = [
            os.path.join(args.path, name)
            for name in sorted(os.listdir(args.path))
            if name.lower().endswith(".mp4")
        ]
        if not paths:
            _fail(f"no .mp4 files found in {args.path}", EXIT_OUTPUT_ERROR)
    else:
        if not os.path.isfile(args.path):
            parser.error(f"media file does not exist: {args.path}")
        paths = [args.path]

    for path in paths:
        result = _upload_one(path)
        print(json.dumps({"path": os.path.abspath(path), **result}, indent=2))


def cmd_upsample(argv):
    parser = argparse.ArgumentParser(
        prog="flow upsample",
        description=(
            "Upsample a finished Flow video to 1080p or 4k — the high-resolution "
            "pass behind the Flow UI's HD download."
        ),
    )
    parser.add_argument("media_id", help="Flow video media ID, or a local video path tracked in history")
    parser.add_argument(
        "--resolution",
        "-R",
        choices=["1080p", "4k"],
        default="1080p",
        help="Target resolution (1080p is free; 4k costs credits and needs a higher tier)",
    )
    parser.add_argument("--aspect", "-a", choices=["portrait", "landscape"], default="landscape")
    parser.add_argument("--output", "-o", default=None, help="Exact output file path")
    parser.add_argument("--seed", type=int, help="Optional explicit upsampler seed")
    parser.add_argument("--idempotency-key", help="Reuse a previous paid request safely")
    args = parser.parse_args(argv)

    _wait_for_generation_ready()
    output_path, explicit_output = _requested_output_path(
        args.output, f"video_{args.resolution}.mp4"
    )
    payload = {
        "media_id": args.media_id,
        "resolution": args.resolution,
        "aspect": args.aspect,
    }
    if args.seed is not None:
        payload["seed"] = args.seed

    idempotency_key = args.idempotency_key or uuid.uuid4().hex
    result = _post_generation("/v1/videos/upsample", payload, idempotency_key, timeout=900)
    result = _poll_video_job(result, idempotency_key)
    _save_outputs(result, output_path, "video", explicit_output=explicit_output)


def cmd_credits(argv):
    parser = argparse.ArgumentParser(prog="flow credits", description="Show Flow credits.")
    parser.parse_args(argv)
    _ensure_backend()
    try:
        data, _ = _request_json("/v1/credits", timeout=15)
    except CliError as error:
        _fail(str(error), error.exit_code)
    if "total_credits" in data:
        print(f"Total Google Flow credits: {data['total_credits']} ({data.get('client_count', 0)} clients)")
    else:
        print(f"Remaining Google Flow credits: {data.get('credits', data)}")


def cmd_status(argv):
    parser = argparse.ArgumentParser(prog="flow status", description="Check backend health.")
    parser.parse_args(argv)
    health = _ensure_backend()
    print(f"Backend: up ({_api_base()})")
    print(f"  status:              {health.get('status')}")
    print(f"  extension_connected: {health.get('extension_connected')}")
    print(f"  has_flow_key:        {health.get('has_flow_key')}")


def cmd_batch(argv):
    _wait_for_generation_ready()
    from flow_server.batch import main as batch_main

    batch_main(argv)


COMMANDS = {
    "mcp": run_mcp,
    "image": cmd_image,
    "batch": cmd_batch,
    "video": cmd_video,
    "upsample": cmd_upsample,
    "edit": cmd_edit,
    "upload": cmd_upload,
    "credits": cmd_credits,
    "status": cmd_status,
}


def _usage():
    print("flow — Flow Agent CLI\n")
    print("Usage:")
    print("  flow [--host HOST] [--port PORT]  Start or reuse the backend")
    print("  flow <command> [args...]\n")
    print("Commands:")
    print("  mcp        Run MCP stdio mode")
    print("  image      Generate images")
    print("  batch      Generate batch images in parallel")
    print("  video      Generate videos")
    print("  upsample   Upsample a video to 1080p or 4k")
    print("  edit       Edit a video")
    print("  upload     Upload media")
    print("  credits    Show Flow credits")
    print("  status     Show backend health")


def main():
    if len(sys.argv) < 2:
        run_backend([])
        return

    first = sys.argv[1]
    if first in ("-h", "--help", "help"):
        _usage()
        return
    if first.startswith("-"):
        run_backend(sys.argv[1:])
        return

    handler = COMMANDS.get(first)
    if handler is None:
        print(f"Unknown command: {first}\n", file=sys.stderr)
        _usage()
        raise SystemExit(2)
    handler(sys.argv[2:])


if __name__ == "__main__":
    main()
