"""In-memory HTTP extension session registry and command queue."""

from __future__ import annotations

import hmac
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Session:
    session_id: str
    secret: str
    flow_key: str | None
    last_seen: float
    queue: deque = field(default_factory=deque)


class ExtensionHttpRegistry:
    """Thread-safe registry for extension HTTP sessions and command queues."""

    def __init__(self, session_ttl_sec: float = 15.0) -> None:
        self._session_ttl_sec = float(session_ttl_sec)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def hello(
        self,
        session_id: str,
        flow_key: str | None = None,
        secret: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register or refresh a session."""
        now = time.monotonic()
        with self._lock:
            self._purge_expired_unlocked(now)
            existing = self._sessions.get(session_id)
            if existing is not None:
                existing.last_seen = now
                if flow_key is not None:
                    existing.flow_key = flow_key
                if secret:
                    existing.secret = secret
                resolved_secret = existing.secret
            else:
                resolved_secret = secret or ""
                self._sessions[session_id] = Session(
                    session_id=session_id,
                    secret=resolved_secret,
                    flow_key=flow_key,
                    last_seen=now,
                )
            return {
                "ok": True,
                "session_id": session_id,
                "secret": resolved_secret,
            }

    def touch(self, session_id: str) -> bool:
        """Refresh last_seen for an existing online session."""
        now = time.monotonic()
        with self._lock:
            self._purge_expired_unlocked(now)
            session = self._get_online_unlocked(session_id, now)
            if session is None:
                return False
            session.last_seen = now
            return True

    def is_connected(self, session_id: str | None = None) -> bool:
        """Return whether the given session (or any session) is online."""
        now = time.monotonic()
        with self._lock:
            self._purge_expired_unlocked(now)
            if session_id is None:
                return self._has_online_unlocked(now)
            return self._get_online_unlocked(session_id, now) is not None

    def has_online_session(self) -> bool:
        return self.is_connected(None)

    def verify_authorization(self, session_id: str, authorization: str | None) -> bool:
        """Constant-time Bearer check against the session secret."""
        candidate = ""
        if isinstance(authorization, str):
            scheme, separator, value = authorization.partition(" ")
            if separator and scheme.lower() == "bearer":
                candidate = value.strip()
        now = time.monotonic()
        with self._lock:
            self._purge_expired_unlocked(now)
            session = self._get_online_unlocked(session_id, now)
            if session is None:
                return False
            return hmac.compare_digest(candidate, session.secret or "")

    def enqueue(self, session_id: str | None, command: dict[str, Any]) -> bool:
        """
        Enqueue a command for a session.

        If session_id is None, deliver to the most recently seen online session.
        Duplicate command ids still present in the session queue are ignored
        (in-flight/queue-only idempotency). After poll removes a command, the
        same id may be enqueued again for business retries.
        """
        now = time.monotonic()
        with self._lock:
            self._purge_expired_unlocked(now)
            session = self._resolve_target_unlocked(session_id, now)
            if session is None:
                return False

            cmd_id = command.get("id")
            if cmd_id is not None:
                cmd_id_str = str(cmd_id)
                if any(str(item.get("id")) == cmd_id_str for item in session.queue):
                    return True

            session.queue.append(dict(command))
            return True

    def poll(self, session_id: str, max_commands: int = 10) -> dict[str, Any]:
        """Pop up to max_commands from the session queue. Each command is delivered once."""
        now = time.monotonic()
        max_n = max(0, int(max_commands))
        with self._lock:
            self._purge_expired_unlocked(now)
            session = self._get_online_unlocked(session_id, now)
            if session is None:
                return {"commands": [], "ok": False, "reason": "session_not_found"}
            session.last_seen = now
            commands: list[dict[str, Any]] = []
            while session.queue and len(commands) < max_n:
                commands.append(session.queue.popleft())
            return {
                "ok": True,
                "commands": commands,
                "session_id": session_id,
                "server_time": int(time.time() * 1000),
            }

    def get_flow_key(self, session_id: str | None = None) -> str | None:
        """Return flow_key for a session, or the most recently seen online session."""
        now = time.monotonic()
        with self._lock:
            self._purge_expired_unlocked(now)
            if session_id is not None:
                session = self._get_online_unlocked(session_id, now)
                return None if session is None else session.flow_key
            latest = self._latest_online_unlocked(now)
            return None if latest is None else latest.flow_key

    def _is_online_unlocked(self, session: Session, now: float) -> bool:
        return (now - session.last_seen) <= self._session_ttl_sec

    def _purge_expired_unlocked(self, now: float) -> None:
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if not self._is_online_unlocked(session, now)
        ]
        for session_id in expired:
            del self._sessions[session_id]

    def _get_online_unlocked(self, session_id: str, now: float) -> Session | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if not self._is_online_unlocked(session, now):
            return None
        return session

    def _has_online_unlocked(self, now: float) -> bool:
        return self._latest_online_unlocked(now) is not None

    def _latest_online_unlocked(self, now: float) -> Session | None:
        latest: Session | None = None
        for session in self._sessions.values():
            if not self._is_online_unlocked(session, now):
                continue
            if latest is None or session.last_seen > latest.last_seen:
                latest = session
        return latest

    def _resolve_target_unlocked(
        self, session_id: str | None, now: float
    ) -> Session | None:
        if session_id is not None:
            return self._get_online_unlocked(session_id, now)
        return self._latest_online_unlocked(now)
