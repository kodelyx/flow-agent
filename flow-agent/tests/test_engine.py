"""Tests for the engine bridge: result extraction and the read-only queries."""

import sqlite3

import pytest

from flow_agent.engine import FlowEngine, clean_cookies, clean_model, extract_json

# --------------------------------------------------------------------------- #
# extract_json
# --------------------------------------------------------------------------- #


def test_extract_json_reads_a_trailing_object_past_the_prose():
    """`generate` announces what it is doing before it prints the result."""
    stdout = (
        "flow-go — generate\n"
        "  submitting…\n"
        "{\n"
        '  "job_id": "abc",\n'
        '  "status": "submitted"\n'
        "}"
    )
    assert extract_json(stdout) == {"job_id": "abc", "status": "submitted"}


def test_extract_json_reads_a_bare_array():
    """`projects --json` marshals a slice, so the value starts with `[`."""
    stdout = (
        "  bridge           not started\n"
        "[\n"
        "  {\n"
        '    "id": "one"\n'
        "  },\n"
        "  {\n"
        '    "id": "two"\n'
        "  }\n"
        "]"
    )
    assert extract_json(stdout) == [{"id": "one"}, {"id": "two"}]


def test_extract_json_returns_the_whole_array_not_its_first_element():
    """The scan runs backwards, so it meets a nested `{` before the outer `[`.

    A parse that is allowed to stop early would answer with one project and look
    entirely valid. This is the assertion that keeps the array case honest.
    """
    stdout = '[\n  {\n    "id": "one"\n  },\n  {\n    "id": "two"\n  }\n]'
    parsed = extract_json(stdout)
    assert isinstance(parsed, list)
    assert len(parsed) == 2


def test_extract_json_ignores_a_line_that_merely_starts_with_a_bracket():
    """`[1/3] rendering` is prose, not the start of an array."""
    stdout = "[1/3] rendering\n{\n  \"ok\": true\n}"
    assert extract_json(stdout) == {"ok": True}


def test_extract_json_reads_compact_empty_values():
    assert extract_json("nothing to report\n[]") == []
    assert extract_json("nothing to report\n{}") == {}


def test_extract_json_raises_when_there_is_no_value():
    with pytest.raises(ValueError):
        extract_json("just prose\nand more prose")


def test_extract_json_raises_on_a_truncated_object():
    """A half-written object is not a result, and must not be guessed at."""
    with pytest.raises(ValueError):
        extract_json('{\n  "job_id": "abc",\n')


# --------------------------------------------------------------------------- #
# argv construction
# --------------------------------------------------------------------------- #


def test_command_drops_arguments_that_were_not_given():
    engine = FlowEngine(root="/tmp", binary="/bin/echo")
    assert engine._command("image", "a boat", None, "", "x2") == [
        "/bin/echo",
        "image",
        "a boat",
        "x2",
    ]


def test_command_keeps_falsy_values_that_are_not_absent():
    """`0` is a value. Only None and "" mean "not given"."""
    engine = FlowEngine(root="/tmp", binary="/bin/echo")
    assert engine._command("image", "p", 0) == ["/bin/echo", "image", "p", "0"]


# --------------------------------------------------------------------------- #
# what reaches the command line
# --------------------------------------------------------------------------- #


def test_clean_model_keeps_a_name_it_was_given():
    """`narwhal` is a model the caller asked for, not a typo to be corrected.

    Rewriting it to `beluga` runs something other than what was asked for, and
    `main.py` passes its own `narwhal` default through untouched, so the CLI and
    the API would disagree about the same word.
    """
    assert clean_model("narwhal") == "narwhal"


def test_clean_model_falls_back_for_a_placeholder():
    """Swagger UI sends `string` for a field left alone."""
    for placeholder in ("", "  ", "string", "default", "none", "null", None):
        assert clean_model(placeholder) == "beluga"


def test_clean_cookies_drops_the_placeholders_a_form_sends():
    """A known-dummy cookie reads as an auth failure, not as an unset option."""
    for placeholder in ("", "   ", "string", "null", "none", "undefined", '""', "''", None):
        assert clean_cookies(placeholder) is None


def test_clean_cookies_strips_the_value_it_keeps():
    """The value becomes one argv token, so padding around it is not cosmetic."""
    assert clean_cookies("  sid=abc  ") == "sid=abc"


async def test_a_placeholder_cookie_never_reaches_the_command(monkeypatch, tmp_path):
    engine = FlowEngine(root=str(tmp_path), binary=str(tmp_path / "flow"))
    seen: dict = {}

    async def capture(*args, **kwargs):
        seen["args"] = args
        return {}

    monkeypatch.setattr(engine, "_run_json", capture)

    await engine.generate_image("a boat", cookies="string")
    assert "--cookies" not in seen["args"]

    await engine.generate_image("a boat", cookies="  sid=abc  ")
    assert seen["args"][-2:] == ("--cookies", "sid=abc")


async def test_a_named_model_reaches_the_command_unchanged(monkeypatch, tmp_path):
    engine = FlowEngine(root=str(tmp_path), binary=str(tmp_path / "flow"))
    seen: dict = {}

    async def capture(*args, **kwargs):
        seen["args"] = args
        return {}

    monkeypatch.setattr(engine, "_run_json", capture)

    await engine.generate_image("a boat", model="narwhal")
    assert seen["args"][seen["args"].index("--model") + 1] == "narwhal"


# --------------------------------------------------------------------------- #
# database reads
# --------------------------------------------------------------------------- #


@pytest.fixture()
def engine_with_db(tmp_path):
    """An engine pointed at a throwaway database with the three tables used."""
    db = tmp_path / "flow.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table accounts (
            account_id text primary key,
            credits integer,
            status text
        );
        create table generations (
            id integer primary key autoincrement,
            job_id text,
            account_id text,
            kind text,
            model text,
            aspect text,
            status text,
            credits_spent integer,
            elapsed_ms integer,
            created_at text
        );
        create table media (id integer primary key autoincrement);
        """
    )
    conn.commit()
    conn.close()

    return FlowEngine(root=str(tmp_path), binary=str(tmp_path / "flow"), db_path=str(db))


def _insert(db_path, table, columns, rows):
    conn = sqlite3.connect(db_path)
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f"insert into {table} ({', '.join(columns)}) values ({placeholders})", rows
    )
    conn.commit()
    conn.close()


def test_read_balance_keeps_unread_distinct_from_empty(engine_with_db):
    """A null balance is "nobody looked", not "no credits".

    Folding it in as zero would understate the total by exactly the accounts
    that have never been checked, which is the one direction that hides a
    problem.
    """
    _insert(
        engine_with_db.db_path,
        "accounts",
        ["account_id", "credits", "status"],
        [("a", 100, "active"), ("b", None, "active"), ("c", 0, "active")],
    )

    report = engine_with_db._read_balance()

    assert [a["credits"] for a in report["accounts"]] == [100, None, 0]
    assert report["total_credits"] == 100
    assert report["unread"] == 1


def test_read_balance_with_no_database(tmp_path):
    engine = FlowEngine(root=str(tmp_path), db_path=str(tmp_path / "absent.db"))
    assert engine._read_balance() == {"accounts": [], "total_credits": 0, "unread": 0}


def test_read_balance_fills_a_missing_status(engine_with_db):
    _insert(
        engine_with_db.db_path,
        "accounts",
        ["account_id", "credits", "status"],
        [("a", 5, None)],
    )
    assert engine_with_db._read_balance()["accounts"][0]["status"] == "unknown"


def test_read_stats_reports_a_credits_total_that_skips_null(engine_with_db):
    """A job that never recorded a cost is skipped, not counted as free."""
    _insert(
        engine_with_db.db_path,
        "generations",
        ["job_id", "account_id", "kind", "model", "aspect", "status",
         "credits_spent", "elapsed_ms", "created_at"],
        [
            ("j1", "a", "image", "narwhal", "1:1", "succeeded", 5, 1000, "2026-01-01"),
            ("j2", "a", "image", "narwhal", "1:1", "empty", None, None, "2026-01-02"),
        ],
    )
    _insert(engine_with_db.db_path, "media", ["id"], [(1,), (2,)])

    report = engine_with_db._read_stats()

    assert report["totals"]["generations"] == 2
    assert report["totals"]["by_status"] == {"succeeded": 1, "empty": 1}
    assert report["totals"]["by_kind"] == {"image": 2}
    assert report["totals"]["media_files"] == 2
    assert report["totals"]["credits_spent"] == 5


def test_read_stats_orders_newest_first_and_converts_milliseconds(engine_with_db):
    _insert(
        engine_with_db.db_path,
        "generations",
        ["job_id", "account_id", "kind", "model", "aspect", "status",
         "credits_spent", "elapsed_ms", "created_at"],
        [
            ("old", "a", "image", "narwhal", "1:1", "succeeded", 1, 2000, "2026-01-01"),
            ("new", "a", "video", "veo", "16:9", "succeeded", 1, 50469, "2026-01-02"),
        ],
    )

    rows = engine_with_db._read_stats()["generations"]

    assert [r["job_id"] for r in rows] == ["new", "old"]
    assert rows[0]["elapsed_seconds"] == pytest.approx(50.469)
    # A job with no timing recorded reports None, not 0.0 — 0.0 reads as
    # "finished instantly", which is the opposite of "never measured".
    assert engine_with_db._read_stats()["generations"][1]["elapsed_seconds"] == 2.0


def test_read_stats_with_a_null_elapsed_reports_none(engine_with_db):
    _insert(
        engine_with_db.db_path,
        "generations",
        ["job_id", "account_id", "kind", "model", "aspect", "status",
         "credits_spent", "elapsed_ms", "created_at"],
        [("j", "a", "image", "narwhal", "1:1", "empty", None, None, "2026-01-01")],
    )
    assert engine_with_db._read_stats()["generations"][0]["elapsed_seconds"] is None


def test_read_stats_with_no_database(tmp_path):
    engine = FlowEngine(root=str(tmp_path), db_path=str(tmp_path / "absent.db"))
    assert engine._read_stats() == {"generations": [], "totals": {}}


def test_the_database_is_opened_read_only(engine_with_db):
    """A status read must not be able to create or lock the engine's database."""
    conn = engine_with_db._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("insert into media (id) values (99)")
    finally:
        conn.close()


def test_available_reflects_the_binary(tmp_path):
    binary = tmp_path / "flow"
    engine = FlowEngine(root=str(tmp_path), binary=str(binary))
    assert engine.available() is False
    binary.write_text("#!/bin/sh\n")
    assert engine.available() is True


def test_find_binary_selects_platform_executable(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # On Windows: prefers flow-windows.exe
    win_bin = bin_dir / "flow-windows.exe"
    win_bin.write_text("dummy")
    monkeypatch.setattr("platform.system", lambda: "Windows")
    assert FlowEngine._find_binary(tmp_path) == win_bin

    # On Linux: prefers flow-linux
    linux_bin = bin_dir / "flow-linux"
    linux_bin.write_text("dummy")
    monkeypatch.setattr("platform.system", lambda: "Linux")
    assert FlowEngine._find_binary(tmp_path) == linux_bin

    # On Darwin: prefers flow-macos
    mac_bin = bin_dir / "flow-macos"
    mac_bin.write_text("dummy")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert FlowEngine._find_binary(tmp_path) == mac_bin
