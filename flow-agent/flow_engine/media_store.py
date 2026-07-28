"""Restart-safe uploaded-media cache backed by the shared history registry.

Filenames are not identities: a path can be overwritten and the same content
can be supplied under many names.  A SHA-256 fingerprint stored on the normal
``history.json`` entry lets upload callers revalidate Google Flow media IDs
without maintaining a second media-ids.json registry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any
from urllib.parse import quote

from .config import ENDPOINTS, HISTORY_FILE, LEGACY_MEDIA_ID_FILE
from flow_server.history import HistoryRegistry, MediaNotFoundError

log = logging.getLogger("flow_engine.media_store")

# Deprecated compatibility alias.  It is deliberately the same history.json
# path, never a separate media-ids.json file.  Keeping it at module scope also
# lets older integrations that monkeypatch MEDIA_ID_FILE continue to work.
MEDIA_ID_FILE = HISTORY_FILE


def _registry() -> HistoryRegistry:
    return HistoryRegistry(MEDIA_ID_FILE, legacy_path=LEGACY_MEDIA_ID_FILE)


def fingerprint(path: str | os.PathLike) -> str:
    """Return a stable SHA-256 fingerprint for a local file."""

    digest = hashlib.sha256()
    with open(os.fspath(path), "rb") as media_file:
        while True:
            chunk = media_file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def get_for_file(path: str | os.PathLike, project_id: str | None = None) -> dict[str, Any] | None:
    """Return the cached history record for matching bytes/project, if present."""

    content_hash = fingerprint(path)
    record = _registry().find(fingerprint=content_hash)
    if not isinstance(record, dict) or not record.get("media_id"):
        return None
    stored_project = record.get("project_id")
    if project_id and stored_project and stored_project != project_id:
        return None
    result = dict(record)
    result["fingerprint"] = content_hash
    return result


def save(
    path: str | os.PathLike,
    media_id: str,
    *,
    project_id: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """Upsert a file-to-media mapping into history.json."""

    if not media_id:
        raise ValueError("media_id is required")
    source_path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"Media file not found: {source_path}")
    content_hash = fingerprint(source_path)
    timestamp = int(time.time())
    media_type = "media"
    if mime_type:
        media_type = "video" if str(mime_type).startswith("video/") else "image"
    record = {
        "type": media_type,
        "prompt": "Uploaded reference file",
        "timestamp": timestamp,
        "media_id": str(media_id),
        "filename": os.path.basename(source_path),
        "url": f"/download/{quote(os.path.basename(source_path), safe='')}",
        "fingerprint": content_hash,
        "size": os.path.getsize(source_path),
        "project_id": project_id,
        "mime_type": mime_type,
        "saved_at": timestamp,
        "source": "uploaded_media_cache",
    }
    stored = _registry().upsert(record)
    log.info("Cached uploaded media in history.json: sha256=%s media_id=%s", content_hash[:12], media_id)
    result = dict(stored)
    result["fingerprint"] = content_hash
    return result


def mark_validated(content_hash: str) -> None:
    """Record successful remote validation without changing the media ID."""

    registry = _registry()
    record = registry.find(fingerprint=content_hash)
    if record is None:
        return
    record["validated_at"] = int(time.time())
    registry.upsert(record)


def invalidate(content_hash: str, media_id: str | None = None) -> bool:
    """Delete a stale cache entry, optionally only if its ID still matches."""

    selectors: dict[str, str] = {"fingerprint": content_hash}
    if media_id is not None:
        selectors["media_id"] = media_id
    try:
        _registry().remove(**selectors)
    except MediaNotFoundError:
        return False
    log.info("Removed stale uploaded-media history entry: sha256=%s", content_hash[:12])
    return True


async def get_revalidated_media_id(
    path: str | os.PathLike,
    bridge,
    project_id: str | None = None,
) -> str | None:
    """Return a cached media ID only after Google Flow confirms it still exists.

    A definite not-found response invalidates the cache.  Transport/server
    failures are surfaced instead of silently re-uploading, because an
    uncertain retry could create duplicate remote assets.
    """

    record = get_for_file(path, project_id=project_id)
    if not record:
        return None
    media_id = record["media_id"]
    endpoint = ENDPOINTS["get_media"].format(media_id=media_id)
    try:
        result = await bridge.api_request(
            endpoint, {}, captcha_action="", method="GET"
        )
    except Exception as error:
        raise RuntimeError(
            f"Could not revalidate cached media ID {media_id}: {error}. "
            "Retry after the Flow extension connection is healthy."
        ) from error

    raw_status = result.get("status", 0) if isinstance(result, dict) else 0
    try:
        status = int(raw_status or 0)
    except (TypeError, ValueError):
        status = 0
    data = result.get("data", result) if isinstance(result, dict) else None
    has_error = isinstance(data, dict) and bool(data.get("error"))
    if status == 200 and not has_error:
        mark_validated(record["fingerprint"])
        log.info(
            "Reusing revalidated media_id=%s for sha256=%s",
            media_id,
            record["fingerprint"][:12],
        )
        return media_id

    error_text = json.dumps(data.get("error", {})).lower() if has_error else ""
    definitely_missing = status in {400, 404, 410} or any(
        marker in error_text for marker in ("not found", "does not exist", "invalid media")
    )
    if definitely_missing:
        invalidate(record["fingerprint"], media_id=media_id)
        return None

    raise RuntimeError(
        f"Could not revalidate cached media ID {media_id}: Flow returned HTTP "
        f"{status or 'unknown'}. Retry when Google Flow is available."
    )


def read_entries() -> dict[str, str]:
    """Compatibility view of all filename-to-media-ID history entries."""

    entries: dict[str, str] = {}
    for record in _registry().load().get("history", []):
        filename = record.get("filename")
        media_id = record.get("media_id")
        if filename and media_id:
            entries[str(filename)] = str(media_id)
    return entries


def write_entries(entries: dict[str, str]) -> None:
    """Compatibility upsert for callers that do not have local file bytes."""

    registry = _registry()
    for filename, media_id in entries.items():
        if filename and media_id:
            registry.upsert(
                {
                    "type": "media",
                    "filename": filename,
                    "media_id": media_id,
                    "prompt": "Registered media ID",
                    "timestamp": int(time.time()),
                    "source": "legacy_compatibility_api",
                }
            )


def get(filename_or_path: str | os.PathLike) -> str | None:
    """Get an ID by file bytes when possible, otherwise by safe filename."""

    path = os.fspath(filename_or_path)
    if os.path.isfile(path):
        record = get_for_file(path)
    else:
        record = _registry().find(filename=path)
    return record.get("media_id") if record else None
