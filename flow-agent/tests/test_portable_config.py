from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_mcp_config_is_documented_only_in_readme():
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert not (REPOSITORY_ROOT / "mcp.json").exists()
    assert '"command": "flow"' in readme
    assert '"args": ["mcp"]' in readme


def test_docs_do_not_embed_user_specific_absolute_paths():
    docs = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "/Users/" not in docs
    assert "C:\\Users\\" not in docs


def test_readme_is_the_only_markdown_setup_guide():
    assert (REPOSITORY_ROOT / "README.md").is_file()
    assert not (REPOSITORY_ROOT / "MCP.md").exists()
    assert "## MCP setup" in (REPOSITORY_ROOT / "README.md").read_text(
        encoding="utf-8"
    )
