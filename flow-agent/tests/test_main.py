"""Tests for the CLI parser.

The parser is the one place a subcommand's arguments are named, so these assert
that every one of them exists and routes to its own handler — a subcommand that
parses but has no `func` fails at the moment a user runs it, which is the worst
time to find out.
"""

import pytest

import main


def parse(*argv):
    return main.build_parser().parse_args(list(argv))


def test_every_subcommand_has_a_handler():
    for command in ("server", "mcp", "image", "video", "balance", "stats", "projects"):
        args = parse(command, *(["x"] if command in ("image", "video") else []))
        assert callable(args.func), f"{command} has no handler"


def test_a_subcommand_is_required():
    with pytest.raises(SystemExit):
        main.build_parser().parse_args([])


def test_image_defaults():
    args = parse("image", "a paper boat")
    assert args.prompt == "a paper boat"
    assert args.aspect == "1:1"
    assert args.count == 1
    assert args.model == "narwhal"
    assert args.all is False


def test_video_defaults():
    args = parse("video", "a paper boat")
    assert args.aspect == "landscape"
    assert args.duration == "8s"
    assert args.quality == "720p"
    assert args.count == 1
    assert args.start_image is None


def test_video_accepts_a_start_image_and_a_count():
    args = parse("video", "x", "--start-image", "frame.png", "--count", "3")
    assert args.start_image == "frame.png"
    assert args.count == 3


def test_server_port_is_an_integer():
    assert parse("server", "--port", "9000").port == 9000


def test_mcp_is_stdio_unless_sse_is_asked_for():
    assert parse("mcp").sse is False
    assert parse("mcp", "--sse").sse is True


def test_balance_refresh_is_opt_in():
    assert parse("balance").refresh is False
    assert parse("balance", "--refresh").refresh is True


def test_the_default_ports_do_not_collide():
    """The HTTP API and the SSE transport are different servers; sharing a port
    would make the second one fail to bind."""
    assert parse("server").port != parse("mcp", "--sse").port
