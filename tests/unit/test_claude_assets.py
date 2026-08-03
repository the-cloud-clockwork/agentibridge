"""Tests for agentibridge.claude_assets MCP client registration."""

import json

import pytest

from agentibridge import claude_assets


@pytest.fixture
def claude_json(tmp_path, monkeypatch):
    """Point the module at a temp ~/.claude.json and a temp env file."""
    path = tmp_path / ".claude.json"
    monkeypatch.setattr(claude_assets, "CLAUDE_JSON", path)
    env_dir = tmp_path / ".agentibridge"
    env_dir.mkdir()
    monkeypatch.setattr(
        claude_assets,
        "_read_env_file_value",
        lambda key, _file=env_dir / "agentibridge.env": _read_env(_file, key),
    )
    return path


def _read_env(env_file, key):
    if not env_file.is_file():
        return ""
    value = ""
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1]
    return value


def _env_file_for(claude_json_path):
    return claude_json_path.parent / ".agentibridge" / "agentibridge.env"


@pytest.mark.unit
class TestInstallMcpStdio:
    def test_default_writes_stdio_entry(self, claude_json):
        claude_assets._install_mcp()

        servers = json.loads(claude_json.read_text())["mcpServers"]
        entry = servers["agentibridge-mcp"]
        assert entry["type"] == "stdio"
        assert entry["args"] == ["serve", "--stdio"]
        assert "url" not in entry


@pytest.mark.unit
class TestInstallMcpSse:
    def test_sse_writes_localhost_url_entry(self, claude_json):
        """The url host must be the literal string localhost — enterprise
        policies silently drop stdio entries AND 127.0.0.1 urls."""
        claude_assets._install_mcp("sse")

        servers = json.loads(claude_json.read_text())["mcpServers"]
        entry = servers["agentibridge-mcp"]
        assert entry["type"] == "sse"
        assert entry["url"] == "http://localhost:8100/sse"
        assert "127.0.0.1" not in entry["url"]
        assert "command" not in entry

    def test_sse_port_from_env_file(self, claude_json):
        env_file = _env_file_for(claude_json)
        env_file.write_text("AGENTIBRIDGE_PORT=9200\n")

        claude_assets._install_mcp("sse")

        entry = json.loads(claude_json.read_text())["mcpServers"]["agentibridge-mcp"]
        assert entry["url"] == "http://localhost:9200/sse"

    def test_sse_api_key_header(self, claude_json):
        env_file = _env_file_for(claude_json)
        env_file.write_text("AGENTIBRIDGE_API_KEYS=first-key,second-key\n")

        claude_assets._install_mcp("sse")

        entry = json.loads(claude_json.read_text())["mcpServers"]["agentibridge-mcp"]
        assert entry["headers"] == {"X-API-Key": "first-key"}

    def test_sse_no_keys_no_headers(self, claude_json):
        claude_assets._install_mcp("sse")

        entry = json.loads(claude_json.read_text())["mcpServers"]["agentibridge-mcp"]
        assert "headers" not in entry


@pytest.mark.unit
class TestTransportSwitching:
    def test_switch_replaces_entry_both_directions(self, claude_json):
        """Same server name in both templates — switching transports must
        replace the entry, never strand the old shape."""
        claude_assets._install_mcp("sse")
        claude_assets._install_mcp("stdio")
        entry = json.loads(claude_json.read_text())["mcpServers"]["agentibridge-mcp"]
        assert entry["type"] == "stdio"
        assert "url" not in entry

        claude_assets._install_mcp("sse")
        entry = json.loads(claude_json.read_text())["mcpServers"]["agentibridge-mcp"]
        assert entry["type"] == "sse"
        assert "command" not in entry

    def test_foreign_servers_untouched(self, claude_json):
        claude_json.write_text(json.dumps({"mcpServers": {"other": {"type": "sse", "url": "http://localhost:1/sse"}}}))

        claude_assets._install_mcp("sse")

        servers = json.loads(claude_json.read_text())["mcpServers"]
        assert "other" in servers
        assert "agentibridge-mcp" in servers
