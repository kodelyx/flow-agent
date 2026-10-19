"""Project character registry, @mention segment parser, and R2V RPC payload builder.

Google Flow binds characters (appearance + connected voice) in video generation
via the `MZZa6b` batchexecute RPC using model `abra_r2v_<duration>`. Rather than
sending a flat prompt string, the RPC expects:
  1. Prompt segments splitting `@<name>` into `[null, [null, null, [id, name]]]`
     and surrounding prose into `[text]`.
  2. A trailing reference list `[[id], ...]` at index 9 of the generation spec.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
from typing import Any, Optional
import urllib.parse
import uuid

import httpx

RPC_GENERATE_R2V = "MZZa6b"
RPC_REGISTER_MEDIA = "as29s"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

ASPECT_TO_CODE = {
    "9:16": 1,
    "portrait": 1,
    "16:9": 2,
    "landscape": 2,
}

DEFAULT_CHARACTERS = [
    {
        "character_id": "82c2dd98-6939-4601-855e-ce1e680a7831",
        "name": "xo",
        "project_id": "ae7210ea-c5e2-4d6d-8560-b55def1edd68",
    }
]


@dataclass(frozen=True)
class CharacterRecord:
    character_id: str
    name: str
    project_id: str = ""

    @property
    def handle(self) -> str:
        return f"@{self.name}"

    def to_dict(self) -> dict[str, str]:
        return {**asdict(self), "handle": self.handle}


class CharacterRepository:
    """Stores and retrieves project character definitions in `data/characters.json`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.store_path = root / "data" / "characters.json"

    def _load_raw(self) -> list[dict[str, str]]:
        if not self.store_path.exists():
            return list(DEFAULT_CHARACTERS)
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else list(DEFAULT_CHARACTERS)
        except (OSError, json.JSONDecodeError):
            return list(DEFAULT_CHARACTERS)

    def _save_raw(self, items: list[dict[str, str]]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def list_characters(self, project_id: Optional[str] = None) -> list[dict[str, str]]:
        records = [CharacterRecord(**item) for item in self._load_raw()]
        if project_id:
            records = [r for r in records if not r.project_id or r.project_id == project_id]
        return [r.to_dict() for r in records]

    def upsert(self, name: str, character_id: str, project_id: str = "") -> dict[str, str]:
        clean_name = name.lstrip("@").strip()
        items = [
            item for item in self._load_raw()
            if item.get("name", "").lower() != clean_name.lower()
            or (project_id and item.get("project_id") not in ("", project_id))
        ]
        record = CharacterRecord(
            character_id=character_id.strip(),
            name=clean_name,
            project_id=project_id.strip(),
        )
        items.append(asdict(record))
        self._save_raw(items)
        return record.to_dict()

    def lookup_map(self, project_id: Optional[str] = None) -> dict[str, CharacterRecord]:
        records = [CharacterRecord(**item) for item in self._load_raw()]
        if project_id:
            records = [r for r in records if not r.project_id or r.project_id == project_id]
        return {r.name.lower(): r for r in records}


class PromptSegmentParser:
    """Splits a prompt containing `@character` tags into Flow R2V RPC segments."""

    MENTION_PATTERN = re.compile(r"@([A-Za-z0-9_-]+)")

    @classmethod
    def parse(
        cls, prompt: str, characters: dict[str, CharacterRecord]
    ) -> tuple[list[Any], list[str]]:
        segments: list[Any] = []
        referenced_ids: list[str] = []
        cursor = 0

        for match in cls.MENTION_PATTERN.finditer(prompt):
            handle_name = match.group(1)
            record = characters.get(handle_name.lower())
            if not record:
                continue

            if match.start() > cursor:
                segments.append([prompt[cursor:match.start()]])

            segments.append([None, [None, None, [record.character_id, record.name]]])
            if record.character_id not in referenced_ids:
                referenced_ids.append(record.character_id)
            cursor = match.end()

        if cursor < len(prompt):
            segments.append([prompt[cursor:]])

        if not segments:
            segments.append([prompt])

        return segments, referenced_ids


class R2VPayloadBuilder:
    """Constructs the `MZZa6b` batchexecute payload for character-bound video generation."""

    @staticmethod
    def _new_id() -> str:
        return str(uuid.uuid4()).upper()

    @classmethod
    def build_inner(
        cls,
        segments: list[Any],
        referenced_ids: list[str],
        project_id: str,
        aspect: str = "landscape",
        duration: str = "8s",
        recaptcha_token: str = "",
        start_media_id: Optional[str] = None,
    ) -> list[Any]:
        dur = duration.split()[0].lower() if duration else "8s"
        dur_clean = "8s" if dur not in ("5s", "8s") else dur
        model_key = f"abra_r2v_{dur_clean}"
        aspect_code = ASPECT_TO_CODE.get(aspect.lower(), 2)
        job_ids = [None, None, None, None, cls._new_id(), cls._new_id()]
        char_refs = [[cid] for cid in referenced_ids]
        start_media_slot = [[None, start_media_id]] if start_media_id else None

        spec = [
            [None, None, [segments]],
            start_media_slot,
            model_key,
            aspect_code,
            None,
            job_ids,
            None,
            None,
            None,
            char_refs,
        ]
        captcha_slot = [recaptcha_token, 1] if recaptcha_token else None
        project_ctx = [
            None, 22, None, None, None, project_id, None, None, None, None, captcha_slot
        ]
        session_slot = [cls._new_id(), 2]
        return [[spec], project_ctx, session_slot]

    @classmethod
    def build_envelope(
        cls,
        segments: list[Any],
        referenced_ids: list[str],
        project_id: str,
        aspect: str = "landscape",
        duration: str = "8s",
        recaptcha_token: str = "",
        start_media_id: Optional[str] = None,
    ) -> list[Any]:
        inner = cls.build_inner(
            segments=segments,
            referenced_ids=referenced_ids,
            project_id=project_id,
            aspect=aspect,
            duration=duration,
            recaptcha_token=recaptcha_token,
            start_media_id=start_media_id,
        )
        inner_json = json.dumps(inner, separators=(",", ":"))
        return [[[RPC_GENERATE_R2V, inner_json, None, "generic"]]]


class AccountBundleReader:
    """Reads and writes synced account credentials in `cookies/*.json` or `data/cookies.json`."""

    @staticmethod
    def resolve_path(cookies_dir: Path, explicit_file: Optional[str] = None) -> Path:
        root_dir = cookies_dir.parent
        if explicit_file:
            path = Path(explicit_file)
            return path if path.is_absolute() else root_dir / path
        if cookies_dir.exists():
            existing = sorted(cookies_dir.glob("*.json"))
            if existing:
                return existing[0]
        return cookies_dir / "cookies.json"

    @classmethod
    def load(cls, cookies_dir: Path, explicit_file: Optional[str] = None) -> Optional[dict[str, Any]]:
        root_dir = cookies_dir.parent
        if explicit_file:
            path = cls.resolve_path(cookies_dir, explicit_file)
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            return None

        candidates: list[Path] = []
        if cookies_dir.exists():
            candidates.extend(sorted(cookies_dir.glob("*.json")))
        data_cookies = root_dir / "data" / "cookies.json"
        if data_cookies.exists():
            candidates.append(data_cookies)

        for file_path in candidates:
            try:
                return json.loads(file_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
        return None

    @classmethod
    def save(
        cls,
        cookies_dir: Path,
        bundle: dict[str, Any],
        explicit_file: Optional[str] = None,
    ) -> Path:
        target = cls.resolve_path(cookies_dir, explicit_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        clean_bundle: dict[str, Any] = {
            "project_id": bundle.get("project_id") or "",
            "at": bundle.get("at") or "",
            "fsid": bundle.get("fsid") or "",
            "cookies": bundle.get("cookies") or [],
        }
        if bundle.get("fingerprint"):
            clean_bundle["fingerprint"] = bundle["fingerprint"]
        target.write_text(json.dumps(clean_bundle, indent=2), encoding="utf-8")
        return target


CAPTCHA_ACTION_VIDEO = "VIDEO_GENERATION"
CAPTCHA_SLOT = "__CAPTCHA__"


class BinaryCaptchaActionResolver:
    """Resolves the reCAPTCHA Enterprise action for Flow video generation."""

    KNOWN_ACTIONS = (
        "VIDEO_GENERATION",
        "IMAGE_GENERATION",
        "FLOW_GENERATION",
    )

    @classmethod
    def resolve(cls, binary_path: Optional[Path]) -> str:
        if binary_path and binary_path.exists():
            try:
                raw = binary_path.read_bytes()
                for candidate in cls.KNOWN_ACTIONS:
                    if candidate.encode("ascii") in raw:
                        return candidate
            except OSError:
                pass
        return CAPTCHA_ACTION_VIDEO


class R2VResponseParser:
    """Parses `batchexecute` `wrb.fr` envelopes for `MZZa6b` operation IDs or errors."""

    @staticmethod
    def parse(raw: str) -> dict[str, Any]:
        if not raw:
            return {"ok": False}
        body = raw.split("\n", 1)[1] if raw.startswith(")]}'") else raw
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(body):
            start = body.find("[", idx)
            if start == -1:
                break
            try:
                chunk, consumed = decoder.raw_decode(body[start:])
            except json.JSONDecodeError:
                idx = start + 1
                continue
            idx = start + consumed
            if not isinstance(chunk, list):
                continue
            for entry in chunk:
                if not isinstance(entry, list) or len(entry) < 3 or entry[0] != "wrb.fr":
                    continue
                if entry[1] != RPC_GENERATE_R2V:
                    continue
                if entry[2] is None:
                    err = entry[5] if len(entry) > 5 else "unknown_error"
                    return {"ok": False, "error": err}
                data = json.loads(entry[2]) if isinstance(entry[2], str) else entry[2]
                op_id = None
                media_id = None
                if isinstance(data, list) and len(data) > 2 and isinstance(data[2], list) and data[2]:
                    first_rec = data[2][0]
                    if isinstance(first_rec, list) and first_rec:
                        op_id = first_rec[0]
                        if len(first_rec) > 3 and isinstance(first_rec[3], list) and len(first_rec[3]) > 4:
                            media_id = first_rec[3][4]
                if not media_id and isinstance(data, list) and len(data) > 3 and isinstance(data[3], list) and data[3]:
                    if isinstance(data[3][0], list) and data[3][0]:
                        media_id = data[3][0][0]
                return {"ok": True, "operation_id": op_id, "media_id": media_id, "data": data}
        return {"ok": False}


class BridgeCaptchaMinter:
    """RFC 6455 WebSocket bridge to execute `MZZa6b` in-page or mint a fresh token."""

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> str:
        head = await reader.readexactly(2)
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
        mask_key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length)
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return payload.decode("utf-8", "replace")

    @staticmethod
    def _encode_frame(text: str) -> bytes:
        data = text.encode("utf-8")
        n = len(data)
        if n < 126:
            header = struct.pack("!BB", 0x81, n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x81, 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 127, n)
        return header + data

    @staticmethod
    def _requests(action: str, freq_template: str) -> list[dict[str, Any]]:
        reqs: list[dict[str, Any]] = [
            {"id": 2, "op": "status"},
            {"id": 3, "op": "flow.at"},
            {"id": 4, "op": "flow.fingerprint"},
            {"id": 5, "op": "cookies.list", "params": {"details": {"domain": "google.com"}}},
            {"id": 7, "op": "cookies.list", "params": {"details": {"domain": "labs.google"}}},
            {"id": 8, "op": "cookies.list", "params": {"details": {"domain": "accounts.google.com"}}},
            {"id": 9, "op": "flow.projects"},
            {"id": 6, "op": "flow.captcha", "params": {"action": action}},
        ]
        if freq_template:
            reqs.insert(
                0,
                {
                    "id": 1,
                    "op": "flow.batch_rpc",
                    "params": {
                        "rpcid": RPC_GENERATE_R2V,
                        "freq": freq_template,
                        "action": action,
                    },
                },
            )
        return reqs

    @staticmethod
    def _extract_project_id_from_url(tab_url: str) -> str:
        match = re.search(r"/project/([A-Za-z0-9_-]+)", tab_url or "")
        return match.group(1) if match else ""

    @classmethod
    def _record_reply(cls, msg: dict[str, Any], result: dict[str, Any]) -> None:
        msg_id = msg.get("id")
        res = msg.get("result")
        if msg_id == 1 and isinstance(res, dict) and res.get("raw"):
            result["batch_rpc"] = res
            if res.get("captcha_minted"):
                result["token_minted"] = True
        elif msg_id == 2 and isinstance(res, dict):
            tab_url = res.get("tabUrl") or ""
            result["tab_url"] = tab_url
            proj_id = cls._extract_project_id_from_url(tab_url)
            if proj_id:
                result["project_id"] = proj_id
        elif msg_id == 3 and isinstance(res, dict):
            result["at"] = res.get("at") or ""
            result["fsid"] = res.get("fsid") or ""
            result["wiz_path"] = res.get("wizPath") or ""
            result["bl"] = res.get("bl") or ""
        elif msg_id == 4 and isinstance(res, dict) and res.get("userAgent"):
            result["fingerprint"] = {
                "user_agent": str(res["userAgent"]),
                "sec_ch_ua": str(res.get("brands") or ""),
                "platform": str(res.get("platformFull") or res.get("platform") or ""),
                "language": str(res.get("language") or "en-US"),
                "mobile": str(res.get("mobile") or "?0"),
            }
        elif msg_id in (5, 7, 8) and isinstance(res, list):
            result.setdefault("cookies", []).extend(res)
        elif msg_id == 9 and isinstance(res, dict) and not result.get("project_id"):
            projects = res.get("projects") or []
            if isinstance(projects, list) and projects and isinstance(projects[0], dict):
                proj_id = str(projects[0].get("id") or "")
                if proj_id:
                    result["project_id"] = proj_id
        elif msg_id == 6 and isinstance(res, dict):
            result["token"] = res.get("token") or ""
            if result["token"]:
                result["token_minted"] = True

    @classmethod
    async def execute_or_mint(
        cls,
        freq_template: str,
        action: str = CAPTCHA_ACTION_VIDEO,
        port: int = 9222,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"action": action}
        done = asyncio.Event()
        reqs = cls._requests(action, freq_template)
        expected_ids = {r["id"] for r in reqs}

        async def handle_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                raw_headers = await reader.readuntil(b"\r\n\r\n")
                key_match = re.search(
                    r"Sec-WebSocket-Key:\s*(\S+)", raw_headers.decode("latin1"), re.I
                )
                if not key_match:
                    return
                accept = base64.b64encode(
                    hashlib.sha1((key_match.group(1) + WS_GUID).encode()).digest()
                ).decode()
                writer.write(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode()
                )
                await writer.drain()

                await cls._read_frame(reader)
                for req in reqs:
                    writer.write(cls._encode_frame(json.dumps(req)))
                await writer.drain()

                seen_ids: set[int] = set()
                for _ in range(20):
                    msg = json.loads(await cls._read_frame(reader))
                    msg_id = msg.get("id")
                    if isinstance(msg_id, int):
                        seen_ids.add(msg_id)
                        cls._record_reply(msg, result)
                    if result.get("batch_rpc") or expected_ids.issubset(seen_ids):
                        done.set()
                        break
            except Exception:
                pass
            finally:
                writer.close()

        try:
            server = await asyncio.start_server(
                handle_client, "127.0.0.1", port, reuse_address=True
            )
        except OSError:
            return result

        async with server:
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        return result

    @staticmethod
    def apply_to_bundle(bundle: dict[str, Any], minted: dict[str, Any]) -> None:
        for key in ("project_id", "at", "fsid", "wiz_path", "bl", "tab_url"):
            if minted.get(key):
                bundle[key] = minted[key]
        if minted.get("fingerprint"):
            bundle["fingerprint"] = minted["fingerprint"]
        if minted.get("cookies"):
            merged: dict[tuple[str, str], dict[str, Any]] = {}
            for cookie in (bundle.get("cookies") or []) + minted["cookies"]:
                if cookie.get("name") and cookie.get("value"):
                    merged[(str(cookie.get("domain", "")), cookie["name"])] = cookie
            bundle["cookies"] = list(merged.values())

    @staticmethod
    def reset_db_session(db_path: Path, clear_all: bool = False) -> None:
        if not db_path.exists():
            return
        import sqlite3
        try:
            with sqlite3.connect(str(db_path), timeout=5) as conn:
                if clear_all:
                    conn.execute("DELETE FROM accounts")
                else:
                    conn.execute("DELETE FROM accounts WHERE status IS NOT NULL AND status NOT IN ('active', 'ok', '')")
                conn.commit()
        except sqlite3.Error:
            pass

    @classmethod
    async def sync_session(
        cls,
        cookies_dir: Path,
        db_path: Path,
        explicit_file: Optional[str] = None,
        timeout: float = 35.0,
    ) -> bool:
        cls.reset_db_session(db_path, clear_all=False)
        minted = await cls.execute_or_mint(freq_template="", timeout=timeout)
        if not minted.get("cookies") and not minted.get("at"):
            return False
        bundle = AccountBundleReader.load(cookies_dir, explicit_file) or {}
        cls.apply_to_bundle(bundle, minted)
        AccountBundleReader.save(cookies_dir, bundle, explicit_file)
        cls.reset_db_session(db_path, clear_all=True)
        await asyncio.sleep(1.5)
        return True


class R2VClient:
    """Submits character-bound `MZZa6b` requests using the active account session."""

    @staticmethod
    def _resolve_base_origin(bundle: dict[str, Any]) -> str:
        tab_url = str(bundle.get("tab_url") or "")
        if tab_url.startswith("https://"):
            parsed = urllib.parse.urlparse(tab_url)
            return f"{parsed.scheme}://{parsed.netloc}"
        for c in bundle.get("cookies") or []:
            if "flow.google.com" in str(c.get("domain", "")):
                return "https://flow.google.com"
        return "https://labs.google"

    @staticmethod
    def _cookie_header(bundle: dict[str, Any]) -> str:
        by_name: dict[str, str] = {}
        for c in bundle.get("cookies") or []:
            if c.get("name") and c.get("value"):
                by_name[c["name"]] = c["value"]
        return "; ".join(f"{k}={v}" for k, v in by_name.items())

    @classmethod
    def _headers(cls, bundle: dict[str, Any], origin: str, project_id: str) -> dict[str, str]:
        fp = bundle.get("fingerprint") or {}
        referer = bundle.get("tab_url") or (
            f"{origin}/fx/tools/flow/project/{project_id}"
            if "labs.google" in origin
            else f"{origin}/project/{project_id}"
        )
        headers = {
            "Accept": "*/*",
            "Accept-Language": fp.get("language") or "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": origin,
            "Referer": referer,
            "Cookie": cls._cookie_header(bundle),
            "X-Same-Domain": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if fp.get("user_agent"):
            headers["User-Agent"] = fp["user_agent"]
        if fp.get("sec_ch_ua"):
            headers["sec-ch-ua"] = fp["sec_ch_ua"]
        if fp.get("platform"):
            headers["sec-ch-ua-platform"] = fp["platform"]
        if fp.get("mobile"):
            headers["sec-ch-ua-mobile"] = fp["mobile"]
        return headers

    @classmethod
    def _candidate_endpoints(cls, origin: str, wiz_path: str) -> list[str]:
        candidates: list[str] = []
        if wiz_path:
            prefix = wiz_path if wiz_path.startswith("/") else f"/{wiz_path}"
            candidates.append(f"{origin}{prefix.rstrip('/')}/data/batchexecute")
        for url in (
            f"{origin}/_/AiSandboxAngularFrontend/data/batchexecute",
            f"{origin}/fx/_/AiSandboxAngularFrontend/data/batchexecute",
        ):
            if url not in candidates:
                candidates.append(url)
        return candidates

    @classmethod
    async def submit(
        cls,
        bundle: dict[str, Any],
        envelope: list[Any],
    ) -> dict[str, Any]:
        origin = cls._resolve_base_origin(bundle)
        project_id = bundle.get("project_id") or ""
        fsid = bundle.get("fsid") or ""
        at_token = bundle.get("at") or ""
        wiz_path = bundle.get("wiz_path") or ""
        bl = bundle.get("bl") or ""

        params: dict[str, str] = {"rpcids": RPC_GENERATE_R2V, "hl": "en", "rt": "c"}
        if project_id:
            params["source-path"] = f"/project/{project_id}"
        if fsid:
            params["f.sid"] = str(fsid)
        if bl:
            params["bl"] = str(bl)

        form: dict[str, str] = {"f.req": json.dumps(envelope, separators=(",", ":"))}
        if at_token:
            form["at"] = str(at_token)

        body = urllib.parse.urlencode(form)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            last_response: Optional[httpx.Response] = None
            for endpoint in cls._candidate_endpoints(origin, wiz_path):
                response = await client.post(
                    endpoint,
                    params=params,
                    content=body,
                    headers=cls._headers(bundle, origin, project_id),
                )
                last_response = response
                if response.status_code not in (404, 405):
                    response.raise_for_status()
                    return {
                        "status_code": response.status_code,
                        "endpoint": endpoint,
                        "raw": response.text[:1000],
                    }

            if last_response is not None:
                last_response.raise_for_status()
            return {"status_code": 500, "raw": "no endpoint reachable"}

    @classmethod
    async def register_media(
        cls,
        bundle: dict[str, Any],
        media_uuid: str,
    ) -> dict[str, Any]:
        """Registers a media UUID with Google Flow via the `as29s` batchexecute RPC."""
        origin = cls._resolve_base_origin(bundle)
        project_id = bundle.get("project_id") or ""
        fsid = bundle.get("fsid") or ""
        at_token = bundle.get("at") or ""
        wiz_path = bundle.get("wiz_path") or ""
        bl = bundle.get("bl") or ""

        params: dict[str, str] = {"rpcids": RPC_REGISTER_MEDIA, "hl": "en", "rt": "c"}
        if project_id:
            params["source-path"] = f"/project/{project_id}"
        if fsid:
            params["f.sid"] = str(fsid)
        if bl:
            params["bl"] = str(bl)

        inner_payload = json.dumps([media_uuid], separators=(",", ":"))
        envelope = [[[RPC_REGISTER_MEDIA, inner_payload, None, "generic"]]]
        form: dict[str, str] = {"f.req": json.dumps(envelope, separators=(",", ":"))}
        if at_token:
            form["at"] = str(at_token)

        body = urllib.parse.urlencode(form)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            last_response: Optional[httpx.Response] = None
            for endpoint in cls._candidate_endpoints(origin, wiz_path):
                response = await client.post(
                    endpoint,
                    params=params,
                    content=body,
                    headers=cls._headers(bundle, origin, project_id),
                )
                last_response = response
                if response.status_code not in (404, 405):
                    response.raise_for_status()
                    return {
                        "status_code": response.status_code,
                        "endpoint": endpoint,
                        "raw": response.text[:1000],
                        "media_id": media_uuid,
                    }

            if last_response is not None:
                last_response.raise_for_status()
            return {"status_code": 500, "raw": "no endpoint reachable"}



