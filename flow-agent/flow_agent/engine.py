"""Async bridge to the Flow engine.

Every generation is one `./bin/flow` invocation. The engine writes its result to
stdout as JSON and its diagnostics to stderr, so this runs the process, keeps the
two apart, and hands back the parsed object.

Nothing here reimplements the engine. The project resolution, the reCAPTCHA
token, the rate-limit cooldown, the account rotation and the SQLite writes all
already exist in the binary — this is a caller, not a second engine.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from .characters import (
    AccountBundleReader,
    CharacterRepository,
    PromptSegmentParser,
    R2VClient,
    R2VPayloadBuilder,
    RPC_GENERATE_R2V,
)

#: How long a generation may take before it is killed. A video render polls for
#: minutes, so this is generous by design rather than a hang guard.
DEFAULT_TIMEOUT = 900


class EngineError(RuntimeError):
    """A Flow engine invocation that did not succeed.

    Carries the process's own words rather than a paraphrase. The engine's
    failures are specific — a spent token, an empty wallet, a session that needs
    refreshing — and each has a different fix, so the message is passed through
    and both streams are kept for a caller that wants to look.
    """

    def __init__(self, command: list[str], returncode: int, stdout: str, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

        detail = (stderr or "").strip() or (stdout or "").strip()
        super().__init__(
            f"{' '.join(command)} exited {returncode}: {detail[-800:]}"
            if detail
            else f"{' '.join(command)} exited {returncode}"
        )


def extract_json(stdout: str) -> dict | list:
    """Pull the result value out of a command's stdout.

    Not `json.loads(stdout)`: the commands print prose before the result.
    `generate` announces what it is about to do, `upload-video` announces what it
    is resolving, and both then marshal the result with `json.MarshalIndent` —
    which puts the opening brace alone on a line. So the last such line is where
    the value starts.

    **Both shapes occur.** `generate` and `upload-video` print an object;
    `projects --json` prints a bare array. Anchoring on either delimiter covers
    the two.

    Two properties make the anchor trustworthy, and both are load-bearing:

    * **The anchor is a line that is *only* the delimiter** (or a line that is a
      complete value on its own). A progress line such as `[1/3] rendering`
      starts with `[` and would otherwise be read as the start of an array.
    * **The value must run to the end of stdout.** The JSON is always the last
      thing a command prints, so requiring the parse to consume everything
      rejects an inner object. Without this, `projects --json` would return its
      *first project* rather than the array of them — the scan runs backwards
      and hits `{` inside the array before it reaches `[`. That failure is
      silent and shape-valid, which is why the rule is asserted rather than
      assumed.

    Raises ValueError when no value parses, which the caller turns into an
    EngineError carrying the command that produced it.
    """
    lines = stdout.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if not stripped.startswith(("{", "[")):
            continue

        # An indented marshal puts the delimiter alone on its line, so the value
        # is everything from here down. A compact value (`[]`, `{}`, a one-line
        # object) is its own line and nothing more.
        candidate = "\n".join(lines[index:]) if stripped in ("{", "[") else stripped

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed

    raise ValueError("no JSON result found on stdout")


class FlowEngine:
    """Runs the flow-go binary and parses what it prints.

    Every path is resolved relative to this package's directory, which is also
    the working directory each invocation runs in — the engine finds `cookies/`,
    `data/` and `output/` relative to its own cwd, so pointing it anywhere else
    would silently use a different set of accounts.
    """

    @staticmethod
    def _find_binary(root: Path, explicit: Optional[str | Path] = None) -> Path:
        """Locate the platform-appropriate engine executable."""
        if explicit:
            return Path(explicit)

        import platform
        system = platform.system().lower()
        bin_dir = root / "bin"

        # Platform priority candidates
        if "windows" in system:
            candidates = ["flow-windows.exe", "flow.exe", "flow"]
        elif "darwin" in system:
            candidates = ["flow-macos", "flow", "flow-darwin"]
        else:  # Linux / Unix
            candidates = ["flow-linux", "flow", "flow-x86_64"]

        for name in candidates:
            candidate_path = bin_dir / name
            if candidate_path.exists():
                return candidate_path

        default_name = "flow-windows.exe" if "windows" in system else "flow"
        return bin_dir / default_name

    def __init__(
        self,
        root: Optional[str | Path] = None,
        binary: Optional[str | Path] = None,
        db_path: Optional[str | Path] = None,
        output_dir: Optional[str | Path] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        if root:
            self.root = Path(root).resolve()
        else:
            pkg_dir = Path(__file__).resolve().parent
            if (pkg_dir / "bin").exists():
                self.root = pkg_dir
            elif (pkg_dir.parent / "bin").exists():
                self.root = pkg_dir.parent
            else:
                self.root = pkg_dir.parent

        self.binary = self._find_binary(self.root, binary)
        self.db_path = Path(db_path) if db_path else self.root / "data" / "flow.db"
        self.output_dir = Path(output_dir) if output_dir else self.root / "output"
        self.cookies_dir = self.root / "cookies"
        self.characters = CharacterRepository(self.root)
        self.timeout = timeout
        self.log_path = self.root / "flow.log"

    def _log(self, message: str) -> None:
        """Append a formatted timestamped entry to flow.log."""
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {message}\n")
        except OSError:
            pass

    # -- process plumbing -------------------------------------------------

    def _command(self, *args: Any) -> list[str]:
        """The argv for one invocation, dropping the arguments that were not given.

        Callers pass `None` or `""` for the options they are not using, and this
        is the one place that turns "not given" into "not on the command line" —
        so no call site has to build its argv conditionally.
        """
        return [str(self.binary)] + [str(a) for a in args if a not in (None, "")]

    @staticmethod
    def _is_expired_session_error(out: str, err: str) -> bool:
        combined = f"{out}\n{err}".lower()
        return (
            "session is no longer usable" in combined
            or "google session expired" in combined
            or "returned 401" in combined
            or "returned no image" in combined
            or "answered with no frames" in combined
        )

    @staticmethod
    def _is_empty_image_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "returned no image" in msg or "answered with no frames" in msg

    async def _exec_binary(self, command: list[str], limit: int) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.root),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), limit)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise EngineError(
                command, -1, "", f"did not finish within {limit}s and was killed"
            ) from None

        out = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace")
        return proc.returncode or 0, out, err

    async def _run(self, *args: Any, timeout: Optional[int] = None) -> tuple[str, str]:
        """Run one command and return (stdout, stderr), raising on a non-zero exit."""
        if not self.binary.exists():
            raise EngineError(
                self._command(*args), 127, "", f"no engine binary at {self.binary}"
            )

        import os
        if hasattr(os, "chmod") and not os.access(self.binary, os.X_OK):
            try:
                os.chmod(self.binary, 0o755)
            except OSError:
                pass

        from .characters import BridgeCaptchaMinter

        BridgeCaptchaMinter.reset_db_session(self.db_path, clear_all=False)
        command = self._command(*args)
        limit = timeout or self.timeout

        code, out, err = await self._exec_binary(command, limit)
        if code != 0 and self._is_expired_session_error(out, err):
            synced = await BridgeCaptchaMinter.sync_session(self.cookies_dir, self.db_path)
            if synced:
                code, out, err = await self._exec_binary(command, limit)

        if code != 0:
            raise EngineError(command, code, out, err)
        return out, err

    async def _run_json(self, *args: Any, timeout: Optional[int] = None) -> dict:
        """Run one command and return its parsed result object."""
        out, _ = await self._run(*args, timeout=timeout)
        try:
            return extract_json(out)
        except ValueError as exc:
            raise EngineError(self._command(*args), 0, out, str(exc)) from exc

    # -- generation -------------------------------------------------------

    def _build_image_args(
        self,
        prompt: str,
        aspect: str,
        count: int,
        model: str,
        all_accounts: bool,
        cookies: Optional[str],
        download: bool,
    ) -> list[Any]:
        args: list[Any] = ["image", prompt, aspect]
        if count and count > 1:
            args.append(f"x{count}")
        if model:
            args += ["--model", model]
        if all_accounts:
            args.append("--all")
        if cookies:
            args += ["--cookies", cookies]
        if not download:
            args.append("--no-download")
        return args

    async def generate_image(
        self,
        prompt: str,
        aspect: str = "1:1",
        count: int = 1,
        model: str = "narwhal",
        all_accounts: bool = False,
        cookies: Optional[str] = None,
        download: bool = True,
        timeout: Optional[int] = None,
    ) -> dict:
        """Generate an image and return the engine's result object."""
        candidates = [model] + [m for m in ("gem_pix_2", "harbor_seal", "narwhal") if m != model]
        last_err: Optional[EngineError] = None
        for candidate_model in candidates:
            args = self._build_image_args(
                prompt, aspect, count, candidate_model, all_accounts, cookies, download
            )
            try:
                return await self._run_json(*args, timeout=timeout)
            except EngineError as exc:
                last_err = exc
                if not self._is_empty_image_error(exc):
                    raise
        raise last_err  # type: ignore[misc]

    async def _resolve_media_id(
        self,
        image_or_id: str,
        cookies: Optional[str] = None,
    ) -> Optional[str]:
        """Upload a local image to Flow media storage or return an existing media ID.
        
        Google Flow expects images uploaded to the project asset store prior to
        binding them as start frames in Reference-to-Video (R2V) generations.
        """
        path = Path(image_or_id)
        if not path.exists():
            return image_or_id

        # 1. Check if the image was already uploaded and recorded in the database
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5) as conn:
                sha256_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                row = conn.execute(
                    "SELECT * FROM media WHERE (sha256 = ? OR file_path = ? OR filename = ?) ORDER BY id DESC LIMIT 1",
                    (sha256_hash, str(path), path.name),
                ).fetchone()
                if row:
                    col_names = [desc[0].lower() for desc in conn.execute("SELECT * FROM media LIMIT 0").description]
                    row_dict = dict(zip(col_names, row))
                    resolved_id = str(row_dict.get("content_id") or row_dict.get("media_id") or "")
                    if resolved_id:
                        self._log(f"MEDIA_CACHE_HIT: resolved {path.name} to {resolved_id}")
                        return resolved_id
        except Exception:
            pass

        from .characters import R2VClient
        bundle = AccountBundleReader.load(self.cookies_dir, cookies)
        if not bundle:
            raise EngineError(["upload-media", str(path)], 1, "", "No account credentials found to upload image.")

        origin = R2VClient._resolve_base_origin(bundle)
        project_id = bundle.get("project_id") or ""
        wiz_path = bundle.get("wiz_path") or ""
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        file_bytes = path.read_bytes()

        base_headers = R2VClient._headers(bundle, origin, project_id)
        # Drop Content-Type so httpx sets proper multipart/form-data boundary.
        base_headers.pop("Content-Type", None)

        upload_endpoints = [
            f"{origin}/upload/media",
            f"{origin}/fx/upload/media",
        ]
        if wiz_path:
            prefix = wiz_path if wiz_path.startswith("/") else f"/{wiz_path}"
            upload_endpoints.insert(0, f"{origin}{prefix.rstrip('/')}/upload/media")

        # 2. Try registering media ID via as29s batchexecute RPC
        media_uuid = str(uuid.uuid4())
        try:
            rpc_res = await R2VClient.register_media(bundle, media_uuid)
            if rpc_res.get("status_code", 500) < 400:
                self._log(f"IMAGE_UPLOAD_SUCCESS: registered {path.name} via as29s RPC -> media_id={media_uuid}")
                # Save to media cache
                try:
                    with sqlite3.connect(str(self.db_path), timeout=5) as conn:
                        sha256_hash = hashlib.sha256(file_bytes).hexdigest()
                        conn.execute(
                            "INSERT OR REPLACE INTO media (media_id, filename, file_path, sha256) VALUES (?, ?, ?, ?)",
                            (media_uuid, path.name, str(path), sha256_hash),
                        )
                        conn.commit()
                except Exception:
                    pass
                return media_uuid
        except Exception as exc:
            self._log(f"IMAGE_UPLOAD_AS29S_ATTEMPT: as29s returned {exc}; continuing to direct transport")

        last_error = ""
        error_details = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            # 1. Attempt multipart/form-data upload across candidate endpoints
            for url in upload_endpoints:
                try:
                    files = {"file": (path.name, file_bytes, mime)}
                    data = {"projectId": project_id} if project_id else {}
                    res = await client.post(url, headers=base_headers, files=files, data=data)
                    if res.status_code < 400:
                        try:
                            res_json = res.json()
                            media_id = res_json.get("media_id") or res_json.get("id") or res_json.get("mediaId")
                            if media_id:
                                self._log(f"IMAGE_UPLOAD_SUCCESS: uploaded {path.name} via {url} -> media_id={media_id}")
                                return str(media_id)
                        except Exception:
                            pass
                    err_entry = f"{url} -> HTTP {res.status_code}: {res.text[:300].strip()}"
                    if res.status_code not in (404, 405) or not last_error:
                        last_error = err_entry
                    error_details.append(err_entry)
                except Exception as exc:
                    err_entry = f"{url} -> {type(exc).__name__}: {exc}"
                    if not last_error:
                        last_error = err_entry
                    error_details.append(err_entry)

            # 2. Attempt resumable Google media upload (scot upload protocol)
            init_headers = dict(base_headers)
            init_headers["X-Goog-Upload-Protocol"] = "resumable"
            init_headers["X-Goog-Upload-Command"] = "start"
            init_headers["X-Goog-Upload-Header-Content-Length"] = str(len(file_bytes))
            init_headers["X-Goog-Upload-Header-Content-Type"] = mime
            init_headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"

            for url in upload_endpoints:
                try:
                    init_res = await client.post(url, headers=init_headers)
                    upload_url = init_res.headers.get("X-Goog-Upload-URL") or init_res.headers.get("Location")
                    if upload_url:
                        upload_headers = {
                            "Content-Type": mime,
                            "X-Goog-Upload-Command": "upload, finalize",
                            "X-Goog-Upload-Offset": "0",
                        }
                        if "Cookie" in base_headers:
                            upload_headers["Cookie"] = base_headers["Cookie"]
                        put_res = await client.post(upload_url, headers=upload_headers, content=file_bytes)
                        if put_res.status_code < 400:
                            put_json = put_res.json()
                            media_id = put_json.get("media_id") or put_json.get("id") or put_json.get("mediaId")
                            if media_id:
                                self._log(f"IMAGE_UPLOAD_SUCCESS: uploaded {path.name} via resumable {url} -> media_id={media_id}")
                                return str(media_id)
                except Exception as exc:
                    error_details.append(f"resumable {url} -> {type(exc).__name__}: {exc}")

        detailed_summary = "; ".join(error_details) if error_details else last_error
        self._log(f"IMAGE_UPLOAD_ERROR: {last_error} (details: {detailed_summary})")
        raise EngineError(["upload-media", str(path)], 1, "", f"Failed to upload {path.name}: {last_error}")

    async def _submit_character_r2v(
        self,
        segments: list[Any],
        referenced_ids: list[str],
        aspect: str,
        duration: str,
        cookies: Optional[str],
        start_media_id: Optional[str] = None,
    ) -> Optional[dict]:
        from .characters import (
            CAPTCHA_SLOT,
            BinaryCaptchaActionResolver,
            BridgeCaptchaMinter,
            R2VResponseParser,
        )

        bundle = AccountBundleReader.load(self.cookies_dir, cookies)
        if not bundle:
            return None

        action = BinaryCaptchaActionResolver.resolve(self.binary)
        project_id = bundle.get("project_id") or ""
        template_envelope = R2VPayloadBuilder.build_envelope(
            segments=segments,
            referenced_ids=referenced_ids,
            project_id=project_id,
            aspect=aspect,
            duration=duration,
            recaptcha_token=CAPTCHA_SLOT,
            start_media_id=start_media_id,
        )
        freq_template = json.dumps(template_envelope, separators=(",", ":"))

        self._log(f"CHARACTER_ATTACH: submitting MZZa6b with freq={freq_template[:300]}...")
        minted = await BridgeCaptchaMinter.execute_or_mint(
            freq_template=freq_template, action=action
        )
        BridgeCaptchaMinter.apply_to_bundle(bundle, minted)
        if minted.get("cookies") or minted.get("at"):
            AccountBundleReader.save(self.cookies_dir, bundle, cookies)

        if minted.get("batch_rpc"):
            rpc_res = minted["batch_rpc"]
            self._log("CHARACTER_ATTACH: executed via extension bridge batch_rpc")
        else:
            envelope = R2VPayloadBuilder.build_envelope(
                segments=segments,
                referenced_ids=referenced_ids,
                project_id=project_id,
                aspect=aspect,
                duration=duration,
                recaptcha_token=minted.get("token", ""),
                start_media_id=start_media_id,
            )
            self._log("CHARACTER_ATTACH: executed via direct R2VClient.submit HTTP")
            rpc_res = await R2VClient.submit(bundle, envelope)

        parsed = R2VResponseParser.parse(str(rpc_res.get("raw", "")))
        if not parsed.get("ok"):
            err_detail = parsed.get("error") or rpc_res.get("raw", "unknown error")
            self._log(f"CHARACTER_ATTACH: r2v_rpc rejected details: {rpc_res.get('raw')!r}")
            raise EngineError(["r2v", RPC_GENERATE_R2V], 1, "", f"R2V RPC submission rejected: {err_detail}")

        out: dict[str, Any] = {
            "status": "submitted",
            "model": f"abra_r2v_{duration}",
            "project_id": project_id,
            "character_ids": referenced_ids,
            "captcha_minted": bool(minted.get("token_minted") or minted.get("token")),
            "captcha_action": action,
            "segments": segments,
            "rpc": rpc_res,
        }
        if parsed.get("operation_id"):
            out["operation_id"] = parsed["operation_id"]
        if parsed.get("media_id"):
            out["media_id"] = parsed["media_id"]
        if start_media_id:
            out["start_image_id"] = start_media_id
        return out

    async def generate_video(
        self,
        prompt: str,
        aspect: str = "landscape",
        duration: str = "8s",
        quality: str = "720p",
        count: int = 1,
        start_image: Optional[str] = None,
        all_accounts: bool = False,
        cookies: Optional[str] = None,
        download: bool = True,
        timeout: Optional[int] = None,
    ) -> dict:
        """Generate a video and return the engine's result object.

        When `@<character_name>` mentions in `prompt` match a registered project
        character, binds the character UUID and voice via the `MZZa6b` (`abra_r2v`)
        structured segment payload.
        """
        char_map = self.characters.lookup_map()
        segments, referenced_ids = PromptSegmentParser.parse(prompt, char_map)
        if referenced_ids:
            start_media_id = None
            try:
                if start_image:
                    start_media_id = await self._resolve_media_id(start_image, cookies)
                    self._log(f"CHARACTER_ATTACH: resolved start_image {start_image!r} to media_id {start_media_id!r}")
            except EngineError as upload_err:
                self._log(f"CHARACTER_ATTACH: upload start_image failed ({upload_err}), falling back to cli generate: {start_image!r}")
                start_media_id = None

            attached = [f"@{k}:{char_map[k].character_id}" for k in char_map if char_map[k].character_id in referenced_ids]
            self._log(f"CHARACTER_ATTACH: prompt={prompt!r} characters={attached} ids={referenced_ids} start_image={start_image!r}")

            try:
                r2v_result = await self._submit_character_r2v(
                    segments=segments,
                    referenced_ids=referenced_ids,
                    aspect=aspect,
                    duration=duration,
                    cookies=cookies,
                    start_media_id=start_media_id,
                )
                if r2v_result is not None:
                    self._log(f"CHARACTER_ATTACH: r2v_rpc submitted successfully: {r2v_result.get('status')}")
                    return r2v_result
            except (httpx.HTTPError, EngineError) as exc:
                self._log(f"CHARACTER_ATTACH: r2v_rpc failed ({exc}), falling back to cli generate")

        args: list[Any] = ["generate", prompt, aspect, duration, quality]
        if start_image:
            args += ["--start-image", str(start_image)]
        if count and count > 1:
            args.append(f"x{count}")
        if all_accounts:
            args.append("--all")
        if cookies:
            args += ["--cookies", cookies]
        if not download:
            args.append("--no-download")

        return await self._run_json(*args, timeout=timeout)

    async def upload_media(
        self,
        file_path: str,
        cookies: Optional[str] = None,
    ) -> str:
        """Upload an image or video to Flow media storage and return its media ID."""
        media_id = await self._resolve_media_id(file_path, cookies)
        if not media_id:
            raise EngineError(["upload-media", file_path], 1, "", f"Failed to upload {file_path}: empty media ID returned")
        return media_id

    async def upload_video(
        self,
        file_path: str,
        cookies: Optional[str] = None,
        force: bool = False,
        timeout: Optional[int] = None,
    ) -> dict:
        """Upload a local video and return the engine's result object.

        The id is `result["media_id"]`, and `result["cached"]` says whether the
        file was already in the project — the engine keys that on a SHA-256 of
        the bytes, so re-uploading the same file is free.
        """
        args: list[Any] = ["upload-video", file_path]
        if force:
            args.append("--force-upload")
        if cookies:
            args += ["--cookies", cookies]

        return await self._run_json(*args, timeout=timeout)

    # -- reads ------------------------------------------------------------

    async def get_balance(self, refresh: bool = False) -> dict:
        """Report every account's credits.

        `refresh` probes upstream first. Either way the figures are read from the
        database, because `balance --refresh` prints a table rather than JSON —
        the probe's job is to write the database, and that is the interface.
        """
        if refresh:
            await self._run("balance", "--refresh")
        return await asyncio.to_thread(self._read_balance)

    async def get_stats(self) -> dict:
        """Report generation history and engine state, read from the database."""
        return await asyncio.to_thread(self._read_stats)

    async def get_projects(self, cookies: Optional[str] = None) -> list[dict]:
        """List the account's Flow projects.

        `--json` is not optional: without it the command renders a table, and a
        table is not something this can read. The flag yields a **bare array**,
        not an object wrapping one.
        """
        args: list[Any] = ["projects", "--json"]
        if cookies:
            args += ["--cookies", cookies]
        out, _ = await self._run(*args)
        try:
            parsed = extract_json(out)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return parsed
        projects = parsed.get("projects") if isinstance(parsed, dict) else None
        return projects if isinstance(projects, list) else []

    async def get_characters(self, project_id: Optional[str] = None) -> list[dict]:
        """List registered Flow project characters (`character_id`, `name`, `handle`)."""
        return await asyncio.to_thread(self.characters.list_characters, project_id)

    async def add_character(
        self, name: str, character_id: str, project_id: str = ""
    ) -> dict:
        """Register or update a character UUID for a Flow project."""
        return await asyncio.to_thread(
            self.characters.upsert, name, character_id, project_id
        )

    # -- database ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open the database read-only.

        Read-only matters twice: it cannot create a database that is not there,
        and it cannot take a write lock on one the engine is using. `mode=ro`
        plus a busy timeout is what keeps a concurrent generation from making a
        status read fail.
        """
        return sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, timeout=5
        )

    def _read_balance(self) -> dict:
        if not self.db_path.exists():
            return {"accounts": [], "total_credits": 0, "unread": 0}

        with self._connect() as conn:
            rows = conn.execute(
                "select account_id, credits, status from accounts order by account_id"
            ).fetchall()

        accounts = [
            {"account_id": account_id, "credits": credits, "status": status or "unknown"}
            for account_id, credits, status in rows
        ]
        # A null balance is "nobody has read it", not zero — the store keeps the
        # two apart on purpose, and folding null in as zero would understate the
        # total by exactly the accounts that have never been checked.
        read = [a for a in accounts if a["credits"] is not None]

        return {
            "accounts": accounts,
            "total_credits": sum(a["credits"] for a in read),
            "unread": len(accounts) - len(read),
        }

    def _read_stats(self) -> dict:
        if not self.db_path.exists():
            return {"generations": [], "totals": {}}

        with self._connect() as conn:
            rows = conn.execute(
                "select job_id, account_id, kind, model, aspect, status, "
                "       credits_spent, elapsed_ms, created_at "
                "from generations order by id desc limit 50"
            ).fetchall()
            by_status = dict(
                conn.execute("select status, count(*) from generations group by status")
            )
            by_kind = dict(
                conn.execute("select kind, count(*) from generations group by kind")
            )
            media_count = conn.execute("select count(*) from media").fetchone()[0]
            # Summed in SQL so a NULL from a job that never finished is skipped
            # rather than counted as free.
            credits_spent = conn.execute(
                "select coalesce(sum(credits_spent), 0) from generations"
            ).fetchone()[0]

        return {
            "generations": [
                {
                    "job_id": job_id,
                    "account_id": account_id,
                    "kind": kind,
                    "model": model,
                    "aspect": aspect,
                    "status": status,
                    "credits_spent": credits_spent,
                    "elapsed_seconds": (elapsed_ms / 1000) if elapsed_ms else None,
                    "created_at": created_at,
                }
                for job_id, account_id, kind, model, aspect, status,
                    credits_spent, elapsed_ms, created_at in rows
            ],
            "totals": {
                "generations": sum(by_status.values()),
                "by_status": by_status,
                "by_kind": by_kind,
                "media_files": media_count,
                "credits_spent": credits_spent,
            },
        }

    # -- misc -------------------------------------------------------------

    def available(self) -> bool:
        """Whether the engine binary is where this expects it."""
        return self.binary.exists()
