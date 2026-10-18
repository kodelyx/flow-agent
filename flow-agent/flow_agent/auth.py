"""API key authentication and access control for the Flow Agent server.

Keys are generated here, stored on disk, revoked here, and verified in constant
time on every inbound request. The OpenAI SDK's ``Authorization: Bearer <key>``
header and the generic ``X-API-Key`` header are both accepted. Credentials are
read from headers only: a query-string credential would be copied into access
logs, browser history and ``Referer`` headers.

The feature is opt-in by construction -- it is a no-op until at least one key
exists, so a fresh clone works with no configuration -- and it fails closed:
a store that exists but cannot be read yields HTTP 503 rather than silently
disabling enforcement.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

KEY_PREFIX = "sk-flow-"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_KEY_FILE = DEFAULT_DATA_DIR / "api_keys.json"
KEY_STORE_VERSION = 1

# auto_error=False keeps Swagger's padlock usable while leaving the meaning of a
# missing credential to verify_api_key (nothing registered => pass through).
security_bearer = HTTPBearer(
    auto_error=False,
    description="OpenAI-style Bearer API key ('sk-flow-...')",
)
security_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="API key sent in the 'X-API-Key' header ('sk-flow-...')",
)


class KeyStoreError(RuntimeError):
    """The key store exists but cannot be read, parsed or trusted.

    Telling this apart from "there is no store" is what makes the failure
    closed: an unreadable store must never look like an empty one.
    """


def get_key_file_path(custom_path: Optional[Path] = None) -> Path:
    """Return the key store path, creating its parent directory if needed."""
    path = custom_path or DEFAULT_KEY_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise KeyStoreError(f"cannot create key store directory {path.parent}: {exc}") from exc
    return path


def generate_secret_key(prefix: str = KEY_PREFIX) -> str:
    """Return a prefixed key carrying 256 bits of URL-safe entropy."""
    return f"{prefix}{secrets.token_urlsafe(32)}"


def mask_key(key: str) -> str:
    """Return a display-safe rendering of ``key`` ('sk-flow-abc1...wxyz')."""
    if len(key) <= 16:
        return "****"
    return f"{key[:12]}...{key[-4:]}"


def load_api_keys(key_file: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return the stored key entries, or ``[]`` when no store exists yet.

    Accepts both the written envelope (``{"version": 1, "keys": [...]}``) and a
    hand-written bare list. Raises :class:`KeyStoreError` when the file exists
    but cannot be read or has no recognisable shape.
    """
    path = get_key_file_path(key_file)
    if not path.exists():
        return []

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KeyStoreError(f"cannot read key store {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise KeyStoreError(f"key store {path} is not valid JSON: {exc}") from exc

    if isinstance(data, dict) and isinstance(data.get("keys"), list):
        entries = data["keys"]
    elif isinstance(data, list):
        entries = data
    else:
        raise KeyStoreError(
            f"key store {path} must be a list of keys or an object holding a 'keys' list"
        )

    return [entry for entry in entries if isinstance(entry, dict)]


def save_api_keys(keys: List[Dict[str, Any]], key_file: Optional[Path] = None) -> None:
    """Write ``keys`` atomically, owner-only readable where the OS allows it."""
    path = get_key_file_path(key_file)
    tmp_path = path.with_suffix(".tmp")
    payload = {
        "version": KEY_STORE_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "keys": keys,
    }

    try:
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass  # Windows and some filesystems cannot express 0600: best effort
        tmp_path.replace(path)
    except OSError as exc:
        raise KeyStoreError(f"cannot write key store {path}: {exc}") from exc


def create_api_key(name: str = "default", key_file: Optional[Path] = None) -> Dict[str, Any]:
    """Create and store a key, returning its metadata.

    The alias is how a later ``revoke`` or ``delete`` names this key, so it has
    to stay unique; a duplicate is refused with :class:`ValueError`.
    """
    alias = name.strip() or "default"
    keys = load_api_keys(key_file)
    if any(entry.get("name") == alias for entry in keys):
        raise ValueError(f"a key named {alias!r} already exists")

    new_entry: Dict[str, Any] = {
        "key": generate_secret_key(),
        "name": alias,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
    }
    keys.append(new_entry)
    save_api_keys(keys, key_file)
    return new_entry


def list_api_keys(key_file: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return the metadata of every stored key."""
    return load_api_keys(key_file)


def _resolve_target(keys: List[Dict[str, Any]], target: str) -> Optional[int]:
    """Return the index of the entry ``target`` names, or ``None`` if absent.

    The key string wins over the alias, and an alias shared by more than one
    entry is refused rather than silently applied to all of them.
    """
    for index, entry in enumerate(keys):
        if entry.get("key") == target:
            return index

    matches = [index for index, entry in enumerate(keys) if entry.get("name") == target]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(
            f"{len(matches)} keys share the alias {target!r}; pass the key string instead"
        )
    return matches[0]


def revoke_api_key(target: str, key_file: Optional[Path] = None) -> bool:
    """Deactivate the key ``target`` names; ``False`` when nothing matches."""
    keys = load_api_keys(key_file)
    index = _resolve_target(keys, target)
    if index is None:
        return False

    keys[index]["active"] = False
    keys[index]["revoked_at"] = datetime.now(timezone.utc).isoformat()
    save_api_keys(keys, key_file)
    return True


def delete_api_key(target: str, key_file: Optional[Path] = None) -> bool:
    """Remove the key ``target`` names; ``False`` when nothing matches."""
    keys = load_api_keys(key_file)
    index = _resolve_target(keys, target)
    if index is None:
        return False

    del keys[index]
    save_api_keys(keys, key_file)
    return True


def get_active_keys(key_file: Optional[Path] = None) -> List[str]:
    """Return every credential that currently authenticates a request.

    That is the ``FLOW_API_KEY`` master key when set, plus every non-revoked
    stored entry. Raises :class:`KeyStoreError` if the store is unreadable.
    """
    active_keys: List[str] = []

    env_master = os.environ.get("FLOW_API_KEY", "").strip()
    if env_master:
        active_keys.append(env_master)

    for entry in load_api_keys(key_file):
        if entry.get("active", True) and entry.get("key"):
            active_keys.append(entry["key"])

    return active_keys


def is_auth_enforced(key_file: Optional[Path] = None) -> bool:
    """Return whether the server requires a credential.

    Enforcement follows from key presence -- an environment master key or at
    least one stored key -- so there is no separate switch. An unreadable store
    reports ``True``: "cannot tell how many keys exist" must never read as
    "no keys exist".
    """
    try:
        return len(get_active_keys(key_file)) > 0
    except KeyStoreError:
        return True


def _constant_time_equals(candidate: str, known: str) -> bool:
    """Compare two credentials without leaking length or content via timing.

    The comparison runs on UTF-8 bytes because ``secrets.compare_digest``
    refuses ``str`` outside ASCII, which would otherwise turn a non-ASCII
    credential into an unhandled ``TypeError`` (HTTP 500).
    """
    return secrets.compare_digest(candidate.encode("utf-8"), known.encode("utf-8"))


def verify_api_key(
    bearer: Optional[HTTPAuthorizationCredentials] = Security(security_bearer),
    x_api_key: Optional[str] = Security(security_header),
) -> Optional[str]:
    """FastAPI dependency that authenticates an inbound request.

    Reads ``Authorization: Bearer <key>`` first, then ``X-API-Key``. Returns the
    presented credential, or ``None`` when nothing is enforced. Raises 401 for a
    missing or unrecognised credential, and 503 when the key store cannot be read.
    """
    try:
        valid_keys = get_active_keys()
    except KeyStoreError as exc:
        # Fail closed: an unreadable store must not degrade into no enforcement.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"API key store is unavailable: {exc}",
        ) from exc

    if not valid_keys:
        return None  # nothing registered: local development stays unauthenticated

    client_token = ""
    if bearer and bearer.credentials:
        client_token = bearer.credentials.strip()
    elif x_api_key:
        client_token = x_api_key.strip()

    if not client_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key: send 'Authorization: Bearer <key>' or 'X-API-Key'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    for valid_key in valid_keys:
        if _constant_time_equals(client_token, valid_key):
            return client_token

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="The supplied API key is invalid or has been revoked.",
        headers={"WWW-Authenticate": "Bearer"},
    )
