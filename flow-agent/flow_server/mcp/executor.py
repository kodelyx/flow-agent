#!/usr/bin/env python3
"""MCP tool dispatch for Flow Agent.

Executes a single MCP tool call and shapes the JSON-RPC result. The tool
implementations reuse the HTTP endpoint functions directly.
"""

import os
import uuid
import time
import json
import base64
import logging
import asyncio
import shutil
import tempfile

from fastapi import HTTPException

from flow_server.config import OUTPUT_DIR, _normalise_model
from flow_server.media_types import extension_for_media, sniff_media_type
from flow_server.models import ImageGenerationRequest, VideoGenerationRequest, VideoUpsampleRequest
from flow_server.mcp.helpers import _mcp_error, _mcp_upload_local_file, _mcp_download_url
from flow_server.routes.system import health, list_models, get_flow_credits
from flow_server.routes.media import get_history
from flow_server.routes.generation import (
    openai_generate_image,
    openai_generate_video,
    openai_upsample_video,
)

log = logging.getLogger("flow_engine.openai_api")


def _resolution_summary(data) -> str:
    """Summarise the delivered resolutions, so a client sees 1080p landed."""
    resolutions = [item.get("resolution") for item in data if item.get("resolution")]
    if not resolutions:
        return ""
    ordered = sorted(set(resolutions), key=resolutions.index)
    return " Resolution: " + ", ".join(ordered) + "."


async def execute_mcp_tool(request_id, tool_name, arguments):
    text = ""
    images_b64 = []

    try:
        if tool_name == "get_flow_status":
            text = json.dumps(await health(), indent=2)

        elif tool_name == "get_flow_credits":
            try:
                # Pass x_client_id explicitly: calling an endpoint function
                # directly leaves FastAPI's Header(...) default in place, which
                # is a truthy object, not None.
                data = await get_flow_credits(x_client_id=None)
            except HTTPException as e:
                return _mcp_error(request_id, -32603, f"Credits unavailable: {e.detail}")
            if isinstance(data, dict) and "total_credits" in data:
                clients = data.get("clients", [])
                ok = sum(1 for c in clients if c.get("ok"))
                failed = len(clients) - ok
                text = (f"Total Google Flow credits across all connected browsers: "
                        f"{data.get('total_credits', 0)} ({ok} clients ready"
                        + (f", {failed} failed" if failed else "") + ")")
            else:
                inner = data.get("data", data) if isinstance(data, dict) else {}
                text = f"Remaining Google Flow credits/generations: {inner.get('credits', 'unknown')}"

        elif tool_name == "list_flow_models":
            data = await list_models()
            models = [item.get("id") for item in data.get("data", [])]
            text = ("Available image models: " + ", ".join(models)
                    + f". Default: {_normalise_model(None)}")

        elif tool_name == "get_flow_history":
            try:
                limit = max(1, min(100, int(arguments.get("limit") or 20)))
            except (TypeError, ValueError):
                limit = 20
            data = await get_history()
            history = (data or {}).get("history", [])[:limit]
            text = json.dumps({"count": len(history), "history": history}, indent=2)

        elif tool_name == "generate_flow_image":
            prompt = str(arguments.get("prompt") or "").strip()
            if not prompt:
                return _mcp_error(request_id, -32602, "Error: 'prompt' is required and cannot be empty.")

            try:
                count = max(1, min(20, int(arguments.get("count") or 1)))
            except (TypeError, ValueError):
                count = 1

            media_ids = list(arguments.get("ref_media_ids") or [])
            local_refs = list(arguments.get("ref_image_paths") or [])
            if arguments.get("ref_image_path"):
                local_refs.insert(0, arguments["ref_image_path"])
            for path in local_refs[:10]:
                try:
                    media_ids.append(await _mcp_upload_local_file(path))
                except Exception as e:
                    return _mcp_error(request_id, -32602, f"Reference upload failed: {str(e)}")

            model = _normalise_model(arguments.get("model"))
            req = ImageGenerationRequest(
                prompt=prompt,
                model=model,
                size=arguments.get("size", "1280x720"),
                n=count,
                ref_media_ids=media_ids[:10] or None,
            )

            res = await openai_generate_image(req, x_client_id=None)
            data = res.get("data", [])
            if not data:
                text = "No images returned by Flow Agent."
            else:
                urls = [item["url"] for item in data if item.get("url")]
                text = f"Success! Generated {len(data)} image(s) with model {model}."
                if urls:
                    text += "\nURLs:\n" + "\n".join(urls)
                for item in data:
                    b64 = item.get("b64_json")
                    if not b64 and item.get("url"):
                        try:
                            local_path = os.path.join(OUTPUT_DIR, item["url"].split("/")[-1])
                            if os.path.exists(local_path):
                                with open(local_path, "rb") as f:
                                    b64 = base64.b64encode(f.read()).decode("utf-8")
                        except Exception as e:
                            log.error(f"Failed to read local image for base64: {e}")
                    if b64:
                        images_b64.append(b64)

        elif tool_name in ("generate_flow_video", "edit_flow_video"):
            prompt = str(arguments.get("prompt") or "").strip()
            if not prompt:
                return _mcp_error(request_id, -32602, "Error: 'prompt' is required and cannot be empty.")

            is_edit = tool_name == "edit_flow_video"
            start_media_id = arguments.get("media_id") if is_edit else arguments.get("start_media_id")

            if is_edit and not start_media_id:
                video_path = arguments.get("video_path")
                if not video_path:
                    return _mcp_error(request_id, -32602, "Error: provide media_id or video_path.")
                try:
                    start_media_id = await _mcp_upload_local_file(video_path)
                except Exception as e:
                    return _mcp_error(request_id, -32602, f"Source video upload failed: {str(e)}")

            start_image_b64 = None
            start_image_path = arguments.get("start_image_path")
            if not is_edit and start_image_path and not start_media_id:
                if not os.path.exists(start_image_path):
                    return _mcp_error(request_id, -32602,
                                      f"Error: Starting image path does not exist: {start_image_path}")
                try:
                    with open(start_image_path, "rb") as f:
                        start_image_b64 = base64.b64encode(f.read()).decode("utf-8")
                except Exception as e:
                    return _mcp_error(request_id, -32602, f"Error reading starting image: {str(e)}")

            try:
                duration = int(arguments.get("duration") or 8)
            except (TypeError, ValueError):
                duration = 8
            try:
                count = 1 if is_edit else max(1, min(20, int(arguments.get("count") or 1)))
            except (TypeError, ValueError):
                count = 1

            req = VideoGenerationRequest(
                prompt=prompt,
                aspect=arguments.get("aspect", "landscape"),
                n=count,
                duration=duration,
                image_base64=start_image_b64,
                start_media_id=start_media_id,
                ref_media_ids=(list(arguments.get("ref_media_ids") or [])[:10] or None),
                is_video=is_edit,
                resolution=arguments.get("resolution"),
            )

            res = await openai_generate_video(req, x_client_id=None)
            data = res.get("data", [])
            if not data:
                text = "No videos returned by Flow Agent."
            else:
                urls = [item.get("url") for item in data if item.get("url")]
                media_ids = [item.get("media_id") for item in data if item.get("media_id")]
                verb = "Edited" if is_edit else "Generated"
                text = (f"Success! {verb} {len(data)} video(s)."
                        + _resolution_summary(data)
                        + ("\nURLs:\n" + "\n".join(urls) if urls else "")
                        + ("\nMedia IDs: " + ", ".join(media_ids) if media_ids else ""))
                if res.get("note"):
                    text += "\nNote: " + res["note"]

        elif tool_name == "upsample_flow_video":
            media_id = str(arguments.get("media_id") or "").strip()
            if not media_id:
                return _mcp_error(request_id, -32602, "Error: 'media_id' is required.")

            seed = arguments.get("seed")
            req = VideoUpsampleRequest(
                media_id=media_id,
                resolution=str(arguments.get("resolution") or "1080p"),
                aspect=arguments.get("aspect", "landscape"),
                seed=int(seed) if seed is not None else None,
            )

            res = await openai_upsample_video(req, x_client_id=None)
            data = res.get("data", [])
            if not data:
                text = "No upsampled video returned by Flow Agent."
            else:
                item = data[0]
                text = (f"Success! Upsampled to {item.get('resolution') or req.resolution}."
                        + (f"\nURL: {item['url']}" if item.get("url") else "")
                        + (f"\nMedia ID: {item['media_id']}" if item.get("media_id") else "")
                        + (f"\nUpsampled from: {item['source_media_id']}"
                           if item.get("source_media_id") else ""))

        elif tool_name == "upload_flow_media":
            file_path = arguments.get("file_path")
            image_url = arguments.get("image_url")
            image_base64 = arguments.get("image_base64")

            if not file_path and not image_url and not image_base64:
                return _mcp_error(
                    request_id, -32602,
                    "Error: You must provide at least one of 'file_path', 'image_url', or 'image_base64'.")

            temp_path = None
            temp_dir = None
            try:
                if file_path:
                    upload_path = file_path
                elif image_base64:
                    payload = image_base64.split(",", 1)[1] if "," in image_base64 else image_base64
                    media_bytes = base64.b64decode("".join(payload.split()), validate=True)
                    mime_type = sniff_media_type(media_bytes)
                    if not mime_type.startswith(("image/", "video/")):
                        raise ValueError(
                            f"Unsupported base64 media signature ({mime_type}); provide an image or video."
                        )
                    ext = extension_for_media(media_bytes)
                    temp_path = os.path.join(
                        OUTPUT_DIR, f"mcp_upload_{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}")
                    with open(temp_path, "wb") as f:
                        f.write(media_bytes)
                    upload_path = temp_path
                else:
                    # Scratch dir, never OUTPUT_DIR: the URL's basename can
                    # collide with a generated asset, and this copy is deleted
                    # after upload — which would take the real file with it.
                    temp_dir = tempfile.mkdtemp(prefix="flow-mcp-upload-")
                    # Off-thread: a blocking fetch here would stall the event
                    # loop, and a URL served by this same backend would deadlock.
                    downloaded = await asyncio.to_thread(
                        _mcp_download_url, image_url, temp_dir, None, 2048)
                    if downloaded.get("error"):
                        return _mcp_error(request_id, -32603, downloaded["error"])
                    upload_path = downloaded["path"]

                media_id = await _mcp_upload_local_file(upload_path)
                text = f"Successfully uploaded asset to Google Flow.\nmedia_id: {media_id}"
            except Exception as e:
                log.exception("Failed to upload media")
                return _mcp_error(request_id, -32603, f"Upload failed: {str(e)}")
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                if temp_dir:
                    shutil.rmtree(temp_dir, ignore_errors=True)

        elif tool_name == "download_media_from_url":
            result = await asyncio.to_thread(
                _mcp_download_url,
                arguments.get("url"),
                arguments.get("output_dir"),
                arguments.get("filename"),
                arguments.get("max_size_mb", 2048),
            )
            if result.get("error"):
                return _mcp_error(request_id, -32603, result["error"])

            if arguments.get("upload_to_flow"):
                try:
                    result["flow_upload"] = {"media_id": await _mcp_upload_local_file(result["path"])}
                except Exception as e:
                    result["flow_upload"] = {"error": str(e)}
            text = json.dumps(result, indent=2)

        else:
            return _mcp_error(request_id, -32601, f"Unknown tool: {tool_name}")

    except HTTPException as e:
        log.exception(f"HTTP error executing tool {tool_name}")
        return _mcp_error(request_id, -32603, f"Tool execution failed: {e.detail}")
    except Exception as e:
        log.exception(f"Exception executing tool {tool_name}")
        return _mcp_error(request_id, -32603, f"Tool execution failed: {str(e)}")

    content = [{"type": "text", "text": text}]
    for b64 in images_b64:
        try:
            image_mime = sniff_media_type(base64.b64decode(b64))
        except Exception:
            image_mime = "application/octet-stream"
        content.append({
            "type": "image",
            "data": b64,
            "mimeType": image_mime
        })

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": content
        }
    }
