"""History-backed upload cache and reference resolution helpers.

``history.json`` is the only durable registry.  This module contains the
bridge-aware consumer logic kept out of the storage-only history core.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from flow_server.media_types import extension_for_media, sniff_media_type

# Kept overrideable for tests and embedders.  The default is resolved lazily
# below to avoid importing flow_engine while its package is initializing.
OUTPUT_DIR: str | None = None


def _history_api():
    """Import lazily while flow_engine may still be initializing."""
    from flow_server import history

    return history


def _output_dir() -> str:
    if OUTPUT_DIR is not None:
        return os.path.abspath(OUTPUT_DIR)
    from flow_engine.config import OUTPUT_DIR as configured_output_dir

    return configured_output_dir


def _public_url(filename: str) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8001").rstrip("/")
    return f"{base}/download/{quote(filename)}"


def _configured_project_id(project_id: str | None = None) -> str:
    if project_id:
        return str(project_id)
    from flow_engine.config import DEFAULT_PROJECT

    return os.environ.get("DEFAULT_PROJECT", DEFAULT_PROJECT)


def fingerprint_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(os.fspath(path), "rb") as media_file:
        while True:
            chunk = media_file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _inside_output_dir(path: str) -> bool:
    output_dir = _output_dir()
    try:
        return os.path.commonpath((os.path.abspath(path), output_dir)) == output_dir
    except ValueError:
        return False


def _archive_input(path: str, fingerprint: str, mime_type: str) -> str:
    """Keep managed input bytes in OUTPUT_DIR, never beside the binary."""
    source = os.path.abspath(path)
    transient_prefixes = ("i2i_upload_", "i2v_upload_", "mcp_upload_")
    if _inside_output_dir(source) and not os.path.basename(source).startswith(transient_prefixes):
        return source

    extension = extension_for_media(source, declared_mime=mime_type)
    output_dir = _output_dir()
    destination = os.path.join(output_dir, f"input_{fingerprint[:16]}{extension}")
    os.makedirs(output_dir, exist_ok=True)
    if not os.path.exists(destination):
        temporary = destination + ".tmp"
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    return destination


def record_uploaded_media(
    path: str | os.PathLike,
    media_id: str,
    *,
    project_id: str | None = None,
    mime_type: str | None = None,
) -> dict:
    """Archive uploaded bytes and upsert their reusable Flow ID in history."""
    source = os.path.abspath(os.fspath(path))
    if not os.path.isfile(source):
        raise FileNotFoundError(f"Media file not found: {source}")
    if not media_id:
        raise ValueError("media_id is required")
    actual_mime = sniff_media_type(source, declared_mime=mime_type)
    fingerprint = fingerprint_file(source)
    stored_path = _archive_input(source, fingerprint, actual_mime)
    return record_local_media(
        stored_path,
        media_id=str(media_id),
        project_id=project_id,
        mime_type=actual_mime,
        fingerprint=fingerprint,
        prompt="Uploaded reference file",
        source="upload",
    )


def record_local_media(
    path: str | os.PathLike,
    *,
    media_id: str | None = None,
    project_id: str | None = None,
    mime_type: str | None = None,
    fingerprint: str | None = None,
    prompt: str = "Saved media",
    source: str = "generated",
    url: str | None = None,
) -> dict:
    """Record a local media artifact without creating another registry."""
    local_path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"Media file not found: {local_path}")
    actual_mime = sniff_media_type(local_path, declared_mime=mime_type)
    filename = os.path.basename(local_path)
    record = {
        "type": "video" if actual_mime.startswith("video/") else "image",
        "prompt": prompt,
        "timestamp": int(time.time()),
        "media_id": str(media_id) if media_id else None,
        "filename": filename,
        "fingerprint": fingerprint or fingerprint_file(local_path),
        "size": os.path.getsize(local_path),
        "project_id": project_id,
        "mime_type": actual_mime,
        "source": source,
    }
    if url:
        record["url"] = str(url)
    elif _inside_output_dir(local_path):
        record["url"] = _public_url(filename)
    else:
        # Explicit output paths remain exact; retain their location as metadata
        # without copying or renaming the user's requested file.
        record["local_path"] = local_path
        record["url"] = Path(local_path).as_uri()
    return _history_api().upsert_history(record)


def _history_local_asset(record: dict) -> str | None:
    """Find an existing local asset referenced by a current or legacy record."""
    for field in ("local_path", "path"):
        raw_path = str(record.get(field) or "").strip()
        if not raw_path:
            continue
        candidate = os.path.abspath(os.path.expanduser(raw_path))
        if os.path.isfile(candidate):
            return candidate

    raw_url = str(record.get("url") or "").strip()
    if raw_url:
        parsed = urlsplit(raw_url)
        if parsed.scheme == "file":
            candidate = os.path.abspath(os.path.expanduser(unquote(parsed.path)))
            if os.path.isfile(candidate):
                return candidate

    # Old generated records usually contain only a /download URL.  Derive one
    # traversal-free basename and look for it directly in FLOW_OUTPUT_DIR.
    filename = _history_api().derive_filename(record)
    if filename:
        candidate = os.path.abspath(os.path.join(_output_dir(), filename))
        try:
            inside_output = os.path.commonpath((candidate, _output_dir())) == _output_dir()
        except ValueError:
            inside_output = False
        if inside_output and os.path.isfile(candidate):
            return candidate
    return None


def _enrich_history_record(
    record: dict,
    local_path: str | None,
    *,
    project_id: str | None,
) -> dict:
    """Recover reuse metadata on older generated-image history entries."""
    refreshed = dict(record)
    if not refreshed.get("project_id"):
        refreshed["project_id"] = _configured_project_id(project_id)

    if local_path:
        actual_mime = sniff_media_type(local_path)
        refreshed.update(
            filename=os.path.basename(local_path),
            mime_type=actual_mime,
            type="video" if actual_mime.startswith("video/") else "image",
            fingerprint=fingerprint_file(local_path),
            size=os.path.getsize(local_path),
        )
        if not refreshed.get("url"):
            if _inside_output_dir(local_path):
                refreshed["url"] = _public_url(os.path.basename(local_path))
            else:
                refreshed["url"] = Path(local_path).as_uri()
                refreshed["local_path"] = local_path

    if refreshed != record:
        return _history_api().upsert_history(refreshed)
    return record


async def _upload_recovered_asset(
    local_path: str,
    bridge,
    *,
    project_id: str,
) -> str:
    """Upload known-local bytes after a definitive remote-media miss."""
    mime_type = sniff_media_type(local_path)
    if mime_type.startswith("image/"):
        from flow_engine.generators.i2v import upload_image

        media_id = await upload_image(
            bridge, local_path, project_id, force_upload=True
        )
    elif mime_type.startswith("video/"):
        from flow_engine.upload import upload_video

        result = await upload_video(
            local_path, project_id, bridge, force_upload=True
        )
        media_id = result.get("mediaId") or result.get("name") or result.get("id")
        if not media_id and isinstance(result.get("media"), dict):
            media_id = result["media"].get("name") or result["media"].get("mediaId")
    else:
        raise RuntimeError(
            f"Cannot re-upload history asset {local_path}: unsupported MIME type {mime_type}."
        )
    if not media_id:
        raise RuntimeError(
            f"History asset {local_path} exists, but Google Flow returned no media_id after upload."
        )
    return str(media_id)


def find_uploaded_file(path: str | os.PathLike, project_id: str | None = None) -> dict | None:
    content_hash = fingerprint_file(path)
    record = _history_api().find_history(fingerprint=content_hash)
    if not record or not record.get("media_id"):
        return None
    stored_project = record.get("project_id")
    if project_id and stored_project and stored_project != project_id:
        return None
    return record


def _response_status(result) -> tuple[int, object, bool]:
    raw_status = result.get("status", 0) if isinstance(result, dict) else 0
    try:
        status = int(raw_status or 0)
    except (TypeError, ValueError):
        status = 0
    data = result.get("data", result) if isinstance(result, dict) else None
    has_error = isinstance(data, dict) and bool(data.get("error"))
    return status, data, has_error


async def revalidate_media_id(media_id: str, bridge) -> bool:
    endpoint = f"/v1/media/{media_id}"
    result = await bridge.api_request(endpoint, {}, captcha_action="", method="GET")
    status, data, has_error = _response_status(result)
    if status == 200 and not has_error:
        return True
    error_text = json.dumps(data.get("error", {})).lower() if has_error else ""
    if status in {400, 404, 410} or any(
        marker in error_text for marker in ("not found", "does not exist", "invalid media")
    ):
        return False
    raise RuntimeError(
        f"Could not revalidate media ID {media_id}: Flow returned HTTP "
        f"{status or 'unknown'}. Retry when Google Flow is available."
    )


async def get_revalidated_upload_id(
    path: str | os.PathLike,
    bridge,
    project_id: str | None = None,
) -> str | None:
    record = find_uploaded_file(path, project_id=project_id)
    if not record:
        return None
    media_id = str(record["media_id"])
    try:
        valid = await revalidate_media_id(media_id, bridge)
    except Exception as exc:
        raise RuntimeError(
            f"Could not revalidate cached media ID {media_id}: {exc}. "
            "Retry after the Flow extension connection is healthy."
        ) from exc
    if not valid:
        return None
    refreshed = dict(record)
    refreshed["validated_at"] = int(time.time())
    _history_api().upsert_history(refreshed)
    return media_id


def _requires_history_lookup(reference: str) -> bool:
    parsed = urlsplit(reference)
    if parsed.scheme in {"http", "https"}:
        return True
    filename = _history_api().safe_filename(reference)
    return bool(filename and os.path.splitext(filename)[1])


async def resolve_media_reference(
    reference: str,
    bridge,
    *,
    expected_type: str | None = None,
    project_id: str | None = None,
) -> str:
    """Resolve an exact history media ID, URL, or filename for reuse.

    A stale remote ID is replaced only after Flow definitively reports it as
    missing.  When an older history entry still has local bytes, those bytes
    are re-uploaded and the same history entry is atomically refreshed.
    """
    value = str(reference or "").strip()
    if not value:
        raise _history_api().MediaNotFoundError()

    record = _history_api().find_history(media_id=value)
    if record is None:
        record = _history_api().find_history(filename=value)
    if record is None and _requires_history_lookup(value):
        raise _history_api().MediaNotFoundError(
            filename=_history_api().safe_filename(value)
        )
    if record is None:
        # Preserve direct Flow IDs, but validate them so an unknown ID returns
        # the same clear 404 as a missing history record instead of failing only
        # after a paid generation request has been submitted.
        try:
            valid = await revalidate_media_id(value, bridge)
        except Exception as exc:
            raise RuntimeError(
                f"Could not validate media ID {value}: {exc}. "
                "Check the Flow extension connection and retry."
            ) from exc
        if valid:
            return value
        raise _history_api().MediaNotFoundError(media_id=value)

    local_path = _history_local_asset(record)
    record = _enrich_history_record(record, local_path, project_id=project_id)
    media_id = record.get("media_id")
    if expected_type and record.get("type") not in {None, "media", expected_type}:
        raise ValueError(
            f"History media {record.get('filename') or media_id} is {record.get('type')}, "
            f"but {expected_type} is required."
        )
    if media_id:
        try:
            valid = await revalidate_media_id(str(media_id), bridge)
        except Exception as exc:
            raise RuntimeError(
                f"Could not revalidate history media ID {media_id}: {exc}. "
                "Check the Flow extension connection and retry."
            ) from exc
        if valid:
            refreshed = dict(record)
            refreshed["validated_at"] = int(time.time())
            _history_api().upsert_history(refreshed)
            return str(media_id)

    if not local_path:
        raise _history_api().MediaNotFoundError(
            filename=record.get("filename"),
            media_id=str(media_id) if media_id else None,
        )

    upload_project = _configured_project_id(project_id or record.get("project_id"))
    try:
        refreshed_media_id = await _upload_recovered_asset(
            local_path,
            bridge,
            project_id=upload_project,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Media ID {media_id or value} is unavailable, and re-uploading "
            f"the history asset {local_path} failed: {exc}"
        ) from exc

    # Restore the generated prompt/source/timestamp after the upload helper's
    # cache upsert, replacing only the stale ID and recovered metadata.
    refreshed = dict(record)
    refreshed.update(
        media_id=refreshed_media_id,
        project_id=upload_project,
        validated_at=int(time.time()),
    )
    refreshed = _enrich_history_record(
        refreshed, local_path, project_id=upload_project
    )
    _history_api().upsert_history(refreshed)
    return refreshed_media_id
