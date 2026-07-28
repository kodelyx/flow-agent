"""Persistent generation-job result storage."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any, Dict, Optional


class GenerationJobStore:
    def __init__(self, output_dir: str):
        self.path = os.path.join(output_dir, ".flow-generation-jobs.json")
        self._lock = asyncio.Lock()

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
                return data
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
        return {"version": 1, "jobs": {}}

    def _write(self, data: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".flow-jobs-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    async def put(self, job_id: str, value: Dict[str, Any]) -> None:
        async with self._lock:
            data = self._read()
            data["jobs"][job_id] = value
            self._write(data)

    async def update(self, job_id: str, **values: Any) -> Optional[Dict[str, Any]]:
        async with self._lock:
            data = self._read()
            job = data["jobs"].get(job_id)
            if not job:
                return None
            job.update(values)
            self._write(data)
            return dict(job)

    async def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            job = self._read()["jobs"].get(job_id)
            return dict(job) if job else None


_stores: Dict[str, GenerationJobStore] = {}


def get_job_store(output_dir: str) -> GenerationJobStore:
    path = os.path.abspath(output_dir)
    store = _stores.get(path)
    if store is None:
        store = GenerationJobStore(path)
        _stores[path] = store
    return store


def clear_job_store_cache() -> None:
    """Forget process-local locks; persisted jobs remain on disk."""
    _stores.clear()
