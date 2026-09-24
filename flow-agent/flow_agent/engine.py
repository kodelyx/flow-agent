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
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

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
        self.timeout = timeout

    # -- process plumbing -------------------------------------------------

    def _command(self, *args: Any) -> list[str]:
        """The argv for one invocation, dropping the arguments that were not given.

        Callers pass `None` or `""` for the options they are not using, and this
        is the one place that turns "not given" into "not on the command line" —
        so no call site has to build its argv conditionally.
        """
        return [str(self.binary)] + [str(a) for a in args if a not in (None, "")]

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

        command = self._command(*args)
        limit = timeout or self.timeout

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
        if proc.returncode != 0:
            raise EngineError(command, proc.returncode or 1, out, err)
        return out, err

    async def _run_json(self, *args: Any, timeout: Optional[int] = None) -> dict:
        """Run one command and return its parsed result object."""
        out, _ = await self._run(*args, timeout=timeout)
        try:
            return extract_json(out)
        except ValueError as exc:
            raise EngineError(self._command(*args), 0, out, str(exc)) from exc

    # -- generation -------------------------------------------------------

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
        """Generate an image and return the engine's result object.

        `aspect` and `count` are the CLI's trailing shorthand, so they go on the
        command line as tokens rather than as flags — `1:1`, `x2` — which is the
        form the engine documents and accepts.

        The result carries `job_id`, `account_id`, `project_id`, `model`,
        `media`, `files` and `status`. `files` is absent when nothing was written,
        which happens with `download=False` and on a refusal alike, so a caller
        that needs the bytes should read it and not assume it.
        """
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

        return await self._run_json(*args, timeout=timeout)

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

        The result carries `media_ids`, `urls`, `files`, `credits_remaining`,
        `quality` and `status`. A render is asynchronous, so with `download=False`
        the ids are returned before the file exists.
        """
        args: list[Any] = ["generate", prompt, aspect, duration, quality]
        if count and count > 1:
            args.append(f"x{count}")
        if start_image:
            args += ["--start-image", start_image]
        if all_accounts:
            args.append("--all")
        if cookies:
            args += ["--cookies", cookies]
        if not download:
            args.append("--no-download")

        return await self._run_json(*args, timeout=timeout)

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
