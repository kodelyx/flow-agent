"""Atomic, backward-compatible registry for generated and uploaded media.

``history.json`` is the single durable media registry.  Besides user-facing
generation fields (URL, prompt, timestamp), an entry may carry upload-cache
metadata such as a content fingerprint and the Google Flow media ID.  Keeping
those fields on the same entry avoids a second registry drifting out of sync.

This module intentionally has no FastAPI or bridge dependencies so CLI,
engine, API, and MCP callers can share it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping
from urllib.parse import quote, unquote, urlsplit

HISTORY_VERSION = 1
_MIGRATION_KEY = "media_id_js"
_JSON_MIGRATION_KEY = "media_ids_json"
_LOCK = threading.RLock()


def _binary_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    argv0 = sys.argv[0] if sys.argv else ""
    if os.path.splitext(os.path.basename(argv0))[0].lower() == "flow":
        resolved = shutil.which(argv0) if not os.path.dirname(argv0) else argv0
        if resolved:
            return os.path.dirname(os.path.abspath(resolved))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_OUTPUT_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("FLOW_OUTPUT_DIR")
    or os.environ.get("OUTPUT_DIR")
    or os.path.join(_binary_dir(), "output")
))
HISTORY_FILE = os.path.abspath(os.path.expanduser(
    os.environ.get("FLOW_HISTORY_FILE", os.path.join(_OUTPUT_DIR, "history.json"))
))
_SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LEGACY_ROOT = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else _SOURCE_ROOT
LEGACY_MEDIA_ID_FILE = os.path.join(_LEGACY_ROOT, "media-id.js")
LEGACY_MEDIA_STORE_FILE = os.path.join(_OUTPUT_DIR, "media-ids.json")


class MediaNotFoundError(LookupError):
    """Raised when a required history media record does not exist."""

    def __init__(
        self,
        *,
        filename: str | None = None,
        media_id: str | None = None,
        fingerprint: str | None = None,
    ) -> None:
        selectors = []
        if filename:
            selectors.append(f"filename={filename!r}")
        if media_id:
            selectors.append(f"media_id={media_id!r}")
        if fingerprint:
            selectors.append(f"fingerprint={fingerprint!r}")
        description = ", ".join(selectors) or "the requested selector"
        super().__init__(
            f"Media not found in history.json ({description}). "
            "Upload or generate it again, then retry."
        )
        self.filename = filename
        self.media_id = media_id
        self.fingerprint = fingerprint


def safe_filename(value: Any, *, required: bool = False) -> str | None:
    """Return a traversal-free basename from a URL, local path, or filename."""

    raw = str(value or "").strip().replace("\x00", "")
    if raw:
        # urlsplit also safely removes query/fragment components.  A Windows
        # path is normalized separately because backslashes are not URL paths.
        parsed = urlsplit(raw)
        candidate = unquote(parsed.path or raw)
        candidate = candidate.replace("\\", "/").rstrip("/")
        filename = os.path.basename(candidate).strip()
        if filename not in {"", ".", ".."}:
            return filename
    if required:
        raise MediaNotFoundError(filename=raw or None)
    return None


def derive_filename(record: Mapping[str, Any], *, required: bool = False) -> str | None:
    """Derive a safe filename from compatible current/legacy record fields."""

    for field in ("filename", "path", "local_path", "url"):
        filename = safe_filename(record.get(field))
        if filename:
            return filename
    if required:
        raise MediaNotFoundError(media_id=_clean_string(record.get("media_id")))
    return None


def _clean_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _empty_document() -> dict[str, Any]:
    return {"version": HISTORY_VERSION, "history": [], "migrations": {}}


def _normalise_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    entry = dict(value)
    filename = derive_filename(entry)
    if filename:
        entry["filename"] = filename
    media_id = _clean_string(entry.get("media_id"))
    if media_id:
        entry["media_id"] = media_id
        # Mappings use the same complete shape as generated records so API and
        # MCP consumers never need to special-case cache-only entries.
        filename = filename or safe_filename(media_id) or media_id
        entry.setdefault("filename", filename)
        entry.setdefault("url", f"/download/{quote(filename, safe='')}")
        entry.setdefault("type", "media")
        entry.setdefault("prompt", "Registered media ID")
        entry.setdefault("timestamp", entry.get("saved_at", int(time.time())))
    elif "media_id" in entry:
        entry["media_id"] = None
    return entry


def _merge_entries(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(old)
    for key, value in new.items():
        # A thin user-facing history append must not erase upload-cache fields
        # already registered for the same media ID.
        if (
            key == "url"
            and isinstance(value, str)
            and value.startswith("/download/")
            and isinstance(merged.get("url"), str)
            and merged["url"]
            and not merged["url"].startswith("/download/")
        ):
            continue
        if value is not None or key not in merged:
            merged[key] = value
    filename = derive_filename(merged)
    if filename:
        merged["filename"] = filename
    return merged


def _deduplicate(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Merge newest-first entries unique by safe filename or media ID."""

    # Replay oldest to newest through upsert semantics.  This preserves cache
    # metadata from an older duplicate while letting the newer real URL,
    # prompt, and timestamp win.
    result: list[dict[str, Any]] = []
    for raw_entry in reversed(list(entries)):
        entry = _normalise_entry(raw_entry)
        if entry is None:
            continue
        filename = derive_filename(entry)
        filename_key = os.path.normcase(filename) if filename else None
        media_id = _clean_string(entry.get("media_id"))
        matches = []
        remaining = []
        for existing in result:
            existing_filename = derive_filename(existing)
            same_filename = bool(
                filename_key
                and existing_filename
                and os.path.normcase(existing_filename) == filename_key
            )
            same_media_id = bool(
                media_id and _clean_string(existing.get("media_id")) == media_id
            )
            if same_filename or same_media_id:
                matches.append(existing)
            else:
                remaining.append(existing)
        merged: dict[str, Any] = {}
        for existing in reversed(matches):
            merged = _merge_entries(merged, existing)
        merged = _merge_entries(merged, entry)
        result = [merged, *remaining]
    return result


def _normalise_document(value: Any) -> dict[str, Any]:
    """Load current history, list-only history, and the retired media store."""

    if isinstance(value, list):
        document = _empty_document()
        document["history"] = _deduplicate(value)
        return document
    if not isinstance(value, Mapping):
        return _empty_document()

    document = dict(value)
    raw_history = document.get("history")
    entries = list(raw_history) if isinstance(raw_history, list) else []

    # Compatibility with the briefly shipped media-ids.json schema.  If such
    # data is supplied as the configured history file, fold it into history.
    old_media = document.pop("media", None)
    if isinstance(old_media, Mapping):
        for fingerprint, raw_record in old_media.items():
            if isinstance(raw_record, Mapping):
                record = dict(raw_record)
                record.setdefault("fingerprint", str(fingerprint))
                record.setdefault("timestamp", record.get("saved_at", int(time.time())))
                entries.append(record)
    old_filenames = document.pop("legacy_filenames", None)
    if isinstance(old_filenames, Mapping):
        for filename, media_id in old_filenames.items():
            if filename and media_id:
                entries.append({"filename": filename, "media_id": media_id})

    document["version"] = HISTORY_VERSION
    document["history"] = _deduplicate(entries)
    if not isinstance(document.get("migrations"), Mapping):
        document["migrations"] = {}
    else:
        document["migrations"] = dict(document["migrations"])
    return document


def _read_legacy_entries(path: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        timestamp = int(os.path.getmtime(path))
        with open(path, "r", encoding="utf-8") as legacy_file:
            for raw_line in legacy_file:
                line = raw_line.strip()
                if " : " not in line:
                    continue
                filename, media_id = line.split(" : ", 1)
                safe_name = safe_filename(filename)
                clean_media_id = _clean_string(media_id)
                if safe_name and clean_media_id:
                    entries.append(
                        {
                            "type": "media",
                            "filename": safe_name,
                            "media_id": clean_media_id,
                            "url": f"/download/{quote(safe_name, safe='')}",
                            "prompt": "Migrated legacy media ID",
                            "timestamp": timestamp,
                            "source": "legacy_media_id_js",
                        }
                    )
    except (FileNotFoundError, OSError):
        pass
    return entries


class HistoryRegistry:
    """Synchronous atomic registry backed by one ``history.json`` file."""

    def __init__(
        self,
        path: str | os.PathLike = HISTORY_FILE,
        *,
        legacy_path: str | os.PathLike | None = LEGACY_MEDIA_ID_FILE,
        legacy_store_path: str | os.PathLike | None = LEGACY_MEDIA_STORE_FILE,
        limit: int | None = None,
    ) -> None:
        self.path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        self.legacy_path = (
            os.path.abspath(os.path.expanduser(os.fspath(legacy_path)))
            if legacy_path is not None
            else None
        )
        self.legacy_store_path = (
            os.path.abspath(os.path.expanduser(os.fspath(legacy_store_path)))
            if legacy_store_path is not None
            else None
        )
        self.limit = max(1, int(limit)) if limit is not None else None

    def _read_unlocked(self) -> tuple[dict[str, Any], bool, list[str]]:
        changed = False
        cleanup_paths: list[str] = []
        try:
            with open(self.path, "r", encoding="utf-8") as history_file:
                raw = json.load(history_file)
            document = _normalise_document(raw)
            changed = document != raw
        except FileNotFoundError:
            document = _empty_document()
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # Preserve availability if an older/corrupt history file is found;
            # the next successful mutation atomically replaces it.
            document = _empty_document()

        migrations = document.setdefault("migrations", {})
        legacy_exists = bool(self.legacy_path and os.path.isfile(self.legacy_path))
        if legacy_exists and not migrations.get(_MIGRATION_KEY):
            legacy_entries = _read_legacy_entries(self.legacy_path)
            document["history"] = _deduplicate(
                list(document.get("history", [])) + legacy_entries
            )
            migrations[_MIGRATION_KEY] = True
            changed = True
        if legacy_exists and migrations.get(_MIGRATION_KEY):
            cleanup_paths.append(self.legacy_path)

        old_store_exists = bool(
            self.legacy_store_path
            and os.path.abspath(self.legacy_store_path) != os.path.abspath(self.path)
            and os.path.isfile(self.legacy_store_path)
        )
        if old_store_exists and not migrations.get(_JSON_MIGRATION_KEY):
            try:
                with open(self.legacy_store_path, "r", encoding="utf-8") as old_store:
                    old_document = _normalise_document(json.load(old_store))
                document["history"] = _deduplicate(
                    list(document.get("history", []))
                    + list(old_document.get("history", []))
                )
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                # An unreadable retired registry must not block history; mark
                # it handled so it cannot remain a competing source of truth.
                pass
            migrations[_JSON_MIGRATION_KEY] = True
            changed = True
        if old_store_exists and migrations.get(_JSON_MIGRATION_KEY):
            cleanup_paths.append(self.legacy_store_path)
        return document, changed, cleanup_paths

    def _write_unlocked(self, document: Mapping[str, Any]) -> None:
        data = _normalise_document(document)
        if self.limit is not None:
            data["history"] = data["history"][: self.limit]
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            prefix=".history-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as history_file:
                json.dump(data, history_file, indent=2, sort_keys=True)
                history_file.write("\n")
                history_file.flush()
                os.fsync(history_file.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass

    def load(self) -> dict[str, Any]:
        """Return compatible history, migrating legacy media IDs once."""

        with _LOCK:
            document, changed, cleanup_paths = self._read_unlocked()
            if changed:
                self._write_unlocked(document)
            self._cleanup_migrated_files(cleanup_paths)
            return document

    @staticmethod
    def _cleanup_migrated_files(paths: Iterable[str]) -> None:
        """Remove retired registries only after history is durably written."""

        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                # The migration marker prevents duplicate imports.  A later
                # load retries cleanup without risking history corruption.
                pass

    def write(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically replace the registry with normalized compatible data."""

        with _LOCK:
            existing, _, cleanup_paths = self._read_unlocked()
            data = _normalise_document(document)
            # A user-facing history rewrite must not discard durable upload
            # mappings. ``clear`` is the explicit operation for deleting them.
            cached_mappings = [
                entry for entry in existing.get("history", []) if entry.get("media_id")
            ]
            data["history"] = _deduplicate(
                list(data.get("history", [])) + cached_mappings
            )
            migrations = dict(existing.get("migrations", {}))
            migrations.update(data.get("migrations", {}))
            data["migrations"] = migrations
            if self.limit is not None:
                data["history"] = data["history"][: self.limit]
            self._write_unlocked(data)
            self._cleanup_migrated_files(cleanup_paths)
            return data

    def upsert(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Insert newest-first, merging matches by filename or media ID."""

        supplied_fields = set(record)
        new_entry = _normalise_entry(record)
        if new_entry is None:
            raise TypeError("history record must be a mapping")
        filename = derive_filename(new_entry)
        media_id = _clean_string(new_entry.get("media_id"))
        if not filename and not media_id:
            raise ValueError("history record requires a filename, URL, path, or media_id")
        new_entry.setdefault("timestamp", int(time.time()))

        with _LOCK:
            document, _, cleanup_paths = self._read_unlocked()
            matches = []
            remaining = []
            filename_key = os.path.normcase(filename) if filename else None
            for existing in document.get("history", []):
                existing_filename = derive_filename(existing)
                same_filename = bool(
                    filename_key
                    and existing_filename
                    and os.path.normcase(existing_filename) == filename_key
                )
                same_media_id = bool(
                    media_id and _clean_string(existing.get("media_id")) == media_id
                )
                if same_filename or same_media_id:
                    matches.append(existing)
                else:
                    remaining.append(existing)

            merged: dict[str, Any] = {}
            for existing in reversed(matches):
                merged = _merge_entries(merged, existing)
            merge_entry = dict(new_entry)
            if matches:
                # Schema defaults make standalone mappings complete, but they
                # are not user intent and must not overwrite richer fields on
                # an existing record.
                for default_field in ("type", "prompt", "timestamp", "url"):
                    if default_field not in supplied_fields:
                        merge_entry.pop(default_field, None)
                if not supplied_fields.intersection({"filename", "path", "local_path", "url"}):
                    merge_entry.pop("filename", None)
            merged = _merge_entries(merged, merge_entry)
            merged = _normalise_entry(merged) or merged
            document["history"] = [merged, *remaining]
            if self.limit is not None:
                document["history"] = document["history"][: self.limit]
            self._write_unlocked(document)
            self._cleanup_migrated_files(cleanup_paths)
            return dict(merged)

    def find(
        self,
        *,
        filename: str | os.PathLike | None = None,
        media_id: str | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Find the first record matching all supplied selectors."""

        safe_name = safe_filename(filename) if filename is not None else None
        clean_media_id = _clean_string(media_id)
        clean_fingerprint = _clean_string(fingerprint)
        if safe_name is None and clean_media_id is None and clean_fingerprint is None:
            return None
        document = self.load()
        for entry in document.get("history", []):
            if safe_name is not None and derive_filename(entry) != safe_name:
                continue
            if clean_media_id is not None and _clean_string(entry.get("media_id")) != clean_media_id:
                continue
            if clean_fingerprint is not None and _clean_string(entry.get("fingerprint")) != clean_fingerprint:
                continue
            return dict(entry)
        return None

    def require(self, **selectors: Any) -> dict[str, Any]:
        """Find a record or raise an actionable ``MediaNotFoundError``."""

        record = self.find(**selectors)
        if record is None:
            raise MediaNotFoundError(
                filename=safe_filename(selectors.get("filename")),
                media_id=_clean_string(selectors.get("media_id")),
                fingerprint=_clean_string(selectors.get("fingerprint")),
            )
        return record

    def remove(self, **selectors: Any) -> dict[str, Any]:
        """Remove one exact match or raise ``MediaNotFoundError``."""

        with _LOCK:
            document, _, cleanup_paths = self._read_unlocked()
            safe_name = safe_filename(selectors.get("filename"))
            media_id = _clean_string(selectors.get("media_id"))
            content_hash = _clean_string(selectors.get("fingerprint"))
            match_index = None
            for index, entry in enumerate(document.get("history", [])):
                if safe_name is not None and derive_filename(entry) != safe_name:
                    continue
                if media_id is not None and _clean_string(entry.get("media_id")) != media_id:
                    continue
                if content_hash is not None and _clean_string(entry.get("fingerprint")) != content_hash:
                    continue
                if any(value is not None for value in (safe_name, media_id, content_hash)):
                    match_index = index
                    break
            if match_index is None:
                raise MediaNotFoundError(
                    filename=safe_name, media_id=media_id, fingerprint=content_hash
                )
            removed = document["history"].pop(match_index)
            self._write_unlocked(document)
            self._cleanup_migrated_files(cleanup_paths)
            return dict(removed)

    def clear(self) -> None:
        """Atomically clear records while preserving completed migrations."""

        with _LOCK:
            document, _, cleanup_paths = self._read_unlocked()
            document["history"] = []
            self._write_unlocked(document)
            self._cleanup_migrated_files(cleanup_paths)


def _registry(
    path: str | os.PathLike | None = None,
    legacy_path: str | os.PathLike | None = LEGACY_MEDIA_ID_FILE,
) -> HistoryRegistry:
    return HistoryRegistry(path or HISTORY_FILE, legacy_path=legacy_path)


def load_history(path: str | os.PathLike | None = None) -> dict[str, Any]:
    return _registry(path).load()


def write_history(
    document: Mapping[str, Any], path: str | os.PathLike | None = None
) -> dict[str, Any]:
    return _registry(path).write(document)


def upsert_history(
    record: Mapping[str, Any], path: str | os.PathLike | None = None
) -> dict[str, Any]:
    return _registry(path).upsert(record)


def find_history(path: str | os.PathLike | None = None, **selectors: Any) -> dict[str, Any] | None:
    return _registry(path).find(**selectors)


def require_history(path: str | os.PathLike | None = None, **selectors: Any) -> dict[str, Any]:
    return _registry(path).require(**selectors)


def remove_history(path: str | os.PathLike | None = None, **selectors: Any) -> dict[str, Any]:
    return _registry(path).remove(**selectors)


def clear_history(path: str | os.PathLike | None = None) -> None:
    _registry(path).clear()
