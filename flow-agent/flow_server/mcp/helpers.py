#!/usr/bin/env python3
"""Shared helpers for the MCP tool layer (errors, uploads, downloads)."""

import os
import tempfile
import urllib.request
import urllib.error
from urllib.parse import urlparse

from flow_engine import DEFAULT_PROJECT

from flow_server.config import OUTPUT_DIR, _safe_filename
from flow_server.media_history import record_local_media
from flow_server.media_types import extension_for_media, sniff_media_type
from flow_server.state import get_active_bridge


def _mcp_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def _mcp_upload_local_file(path):
    """Upload a local image/video to Flow. Returns a media_id or raises."""
    if not os.path.exists(path):
        raise ValueError(f"File path does not exist: {path}")

    active_bridge = await get_active_bridge()
    project_id = os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)

    mime_type = sniff_media_type(path)
    if mime_type.startswith("video/"):
        from flow_engine.upload import upload_video
        upload_res = await upload_video(path, project_id, active_bridge)
        media_id = upload_res.get("mediaId") or upload_res.get("name") or upload_res.get("id")
        if not media_id and isinstance(upload_res.get("media"), dict):
            media_id = upload_res["media"].get("name") or upload_res["media"].get("mediaId")
    elif mime_type.startswith("image/"):
        from flow_engine.generators.i2v import upload_image
        media_id = await upload_image(active_bridge, path, project_id)
    else:
        raise ValueError(f"Unsupported local media type {mime_type}: {path}")

    if not media_id:
        raise ValueError("Failed to upload asset to Google Flow.")
    return media_id


def _mcp_download_url(url, output_dir=None, filename=None, max_size_mb=2048):
    """Blocking download of a remote media asset. Returns a result dict."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"error": "Only valid http:// or https:// URLs are supported."}

    try:
        max_bytes = max(1, min(4096, int(max_size_mb or 2048))) * 1024 * 1024
    except (TypeError, ValueError):
        max_bytes = 2048 * 1024 * 1024

    destination = os.path.abspath(os.path.expanduser(output_dir or OUTPUT_DIR))
    os.makedirs(destination, exist_ok=True)
    request = urllib.request.Request(
        str(url).strip(),
        headers={"User-Agent": "Mozilla/5.0 (Flow Agent MCP)",
                 "Accept": "image/*,video/*,application/octet-stream,*/*"},
    )
    temp_path = None
    try:
        # Bypass any ambient proxy: signed CDN links often break through one.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=120) as response:
            declared_type = response.headers.get("Content-Type", "")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                return {"error": f"Download exceeds max_size_mb ({max_size_mb} MB)."}

            url_name = _safe_filename(os.path.basename(parsed.path))
            output_name = _safe_filename(filename or url_name)

            fd, temp_path = tempfile.mkstemp(prefix=".flow-download-", dir=destination)
            total = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        return {"error": f"Download exceeded max_size_mb ({max_size_mb} MB)."}
                    out.write(chunk)

            content_type = sniff_media_type(
                temp_path, declared_mime=declared_type, filename=url_name
            )
            actual_extension = extension_for_media(
                temp_path, declared_mime=declared_type, filename=url_name
            )
            if actual_extension:
                stem, existing_extension = os.path.splitext(output_name)
                if existing_extension.lower() != actual_extension:
                    output_name = (stem if existing_extension else output_name) + actual_extension
            final_path = os.path.join(destination, output_name)
            os.replace(temp_path, final_path)
            temp_path = None

        if content_type.startswith(("image/", "video/")):
            record_local_media(
                final_path,
                mime_type=content_type,
                prompt=f"Downloaded from {parsed.netloc}",
                source="download",
            )

        return {
            "success": True,
            "path": final_path,
            "bytes": total,
            "content_type": content_type,
            "source_host": parsed.netloc,
        }
    except urllib.error.HTTPError as e:
        return {"error": f"Download failed ({e.code}): {e.reason}"}
    except Exception as e:
        return {"error": f"Download failed: {str(e)}"}
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
