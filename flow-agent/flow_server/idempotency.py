"""Durable idempotency records for generation requests.

Generation calls consume Flow credits.  A retry after a client timeout must
therefore replay the first call instead of submitting another generation.
The small JSON store below is deliberately dependency-free and is written
atomically so records survive a backend restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class IdempotencyClaim:
    action: str
    record: Dict[str, Any]


class IdempotencyStore:
    def __init__(self, output_dir: str):
        self.path = os.path.join(output_dir, ".flow-idempotency.json")
        self._lock = asyncio.Lock()

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                return data
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
        return {"version": 1, "entries": {}}

    def _write(self, data: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".flow-idempotency-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    @staticmethod
    def _key_id(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint(payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def claim(
        self,
        key: str,
        payload: Dict[str, Any],
        *,
        job_id: Optional[str] = None,
        created: Optional[int] = None,
    ) -> IdempotencyClaim:
        """Atomically reserve *key* or describe the existing reservation."""
        key_id = self._key_id(key)
        fingerprint = self.fingerprint(payload)
        async with self._lock:
            data = self._read()
            existing = data["entries"].get(key_id)
            if existing:
                if existing.get("fingerprint") != fingerprint:
                    return IdempotencyClaim("conflict", existing)
                state = existing.get("state", "processing")
                if state == "succeeded":
                    return IdempotencyClaim("replay", existing)
                if state == "failed":
                    return IdempotencyClaim("failed", existing)
                return IdempotencyClaim("processing", existing)

            record = {
                "fingerprint": fingerprint,
                "state": "processing",
                "job_id": job_id,
                "created": created,
            }
            data["entries"][key_id] = record
            self._write(data)
            return IdempotencyClaim("execute", record)

    async def succeed(self, key: str, response: Dict[str, Any]) -> None:
        await self._finish(key, "succeeded", response=response)

    async def fail(self, key: str, status_code: int, detail: str) -> None:
        await self._finish(
            key,
            "failed",
            error={"status_code": status_code, "detail": detail},
        )

    async def _finish(self, key: str, state: str, **values: Any) -> None:
        key_id = self._key_id(key)
        async with self._lock:
            data = self._read()
            record = data["entries"].get(key_id)
            if not record:
                return
            record.update(values)
            record["state"] = state
            self._write(data)


_stores: Dict[str, IdempotencyStore] = {}


def get_idempotency_store(output_dir: str) -> IdempotencyStore:
    path = os.path.abspath(output_dir)
    store = _stores.get(path)
    if store is None:
        store = IdempotencyStore(path)
        _stores[path] = store
    return store


def clear_idempotency_store_cache() -> None:
    """Forget process-local locks; persisted records remain on disk."""
    _stores.clear()
