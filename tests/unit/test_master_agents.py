"""Unit tests for master agents opened through agentihooks init-agent."""

import asyncio
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import agentibridge.master_agents as ma

ROUTED = "host=herdr\nname=master-1\nstatus=started\nroute_status=routed\naccount=acct-a\npane_id=p-1\n"


def _done(returncode: int, stdout: str, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["agentihooks"], returncode, stdout, stderr)


@pytest.fixture
def paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ma, "AGENTIHOOKS_BINARY", "/opt/hooks/bin/agentihooks")
    monkeypatch.setattr(ma, "AGENTIBRIDGE_MASTER_DIR", "/work/eco")


@pytest.mark.unit
class TestLaunchCommand:
    def test_routes_claude_ai_with_remote_control(self, paths: None) -> None:
        assert ma.launch_command("master-1", "") == [
            "/opt/hooks/bin/agentihooks",
            "init-agent",
            "--agent",
            "claude",
            "--dir",
            "/work/eco",
            "--name",
            "master-1",
            "--",
            "--route",
            "claude-ai",
            "--remote-control",
            "master-1",
        ]

    def test_opening_prompt_goes_to_init_agent(self, paths: None) -> None:
        command = ma.launch_command("master-1", "check the swarm")

        assert command[command.index("--prompt") + 1] == "check the swarm"
        assert command.index("--prompt") < command.index("--")


@pytest.mark.unit
class TestLaunchEnvironment:
    def test_drops_anthropic_overrides_and_puts_agentihooks_first(
        self, paths: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-under-test")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.invalid")
        monkeypatch.setenv("PATH", "/usr/bin")

        env = ma.launch_environment()

        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "ANTHROPIC_BASE_URL" not in env
        assert env["PATH"] == "/opt/hooks/bin:/usr/bin"

    def test_bare_binary_adds_nothing_to_path(self, paths: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ma, "AGENTIHOOKS_BINARY", "agentihooks")
        monkeypatch.setenv("PATH", "/usr/bin")

        assert ma.launch_environment()["PATH"] == "/usr/bin"


@pytest.mark.unit
class TestLaunchMaster:
    def test_routed_launch_succeeds(self, paths: None) -> None:
        with patch.object(ma.subprocess, "run", return_value=_done(0, ROUTED)) as run:
            result = ma.launch_master("master-1")

        assert result == {
            "success": True,
            "name": "master-1",
            "directory": "/work/eco",
            "report": {
                "host": "herdr",
                "name": "master-1",
                "status": "started",
                "route_status": "routed",
                "account": "acct-a",
                "pane_id": "p-1",
            },
        }
        assert run.call_args.args[0] == ma.launch_command("master-1", "")
        assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL
        assert run.call_args.kwargs["timeout"] == ma.LAUNCH_TIMEOUT_S

    def test_failed_launch_reports_stderr(self, paths: None) -> None:
        failed = _done(2, "", "agentihooks init-agent: herdr is down\n")
        with patch.object(ma.subprocess, "run", return_value=failed):
            result = ma.launch_master("master-1")

        assert result["success"] is False
        assert result["error"] == "agentihooks init-agent: herdr is down"

    def test_silent_nonzero_exit_reports_the_exit_code(self, paths: None) -> None:
        with patch.object(ma.subprocess, "run", return_value=_done(2, "")):
            result = ma.launch_master("master-1")

        assert result["error"] == "init-agent exited 2, route_status=none"

    def test_route_error_fails_the_launch(self, paths: None) -> None:
        refused = _done(0, "status=started\nroute_status=refused\nroute_error=no usable claude ai login\n")
        with patch.object(ma.subprocess, "run", return_value=refused):
            result = ma.launch_master("master-1")

        assert result["success"] is False
        assert result["error"] == "no usable claude ai login"

    def test_unrouted_launch_fails(self, paths: None) -> None:
        with patch.object(ma.subprocess, "run", return_value=_done(0, "status=started\nroute_status=pending\n")):
            result = ma.launch_master("master-1")

        assert result["success"] is False
        assert result["error"] == "init-agent exited 0, route_status=pending"

    @pytest.mark.parametrize(
        "raised", [FileNotFoundError("agentihooks"), subprocess.TimeoutExpired("agentihooks", ma.LAUNCH_TIMEOUT_S)]
    )
    def test_launch_that_raises_fails(self, paths: None, raised: Exception) -> None:
        with patch.object(ma.subprocess, "run", side_effect=raised):
            result = ma.launch_master("master-1")

        assert result == {"success": False, "name": "master-1", "directory": "/work/eco", "error": str(raised)}

    def test_unnamed_launch_gets_a_master_name(self, paths: None) -> None:
        with patch.object(ma.subprocess, "run", return_value=_done(0, ROUTED)) as run:
            result = ma.launch_master()

        assert re.fullmatch(r"master-\d{6}-\d{6}", result["name"])
        assert run.call_args.args[0][-1] == result["name"]


@pytest.mark.unit
class TestInitInteractiveAgentTool:
    def test_registered_under_its_name(self) -> None:
        import agentibridge.server as srv

        assert "init-interactive-agent" in [tool.name for tool in srv.mcp._tool_manager.list_tools()]

    def test_returns_the_launch_result(self) -> None:
        import agentibridge.server as srv

        launched = {"success": True, "name": "master-1"}
        with patch("agentibridge.master_agents.launch_master", return_value=launched) as launch:
            result = json.loads(asyncio.run(srv.init_interactive_agent(name="master-1", prompt="hello")))

        assert result == launched
        launch.assert_called_once_with("master-1", "hello")


@pytest.mark.unit
class TestMasterSettings:
    @pytest.mark.parametrize("value", [None, ""])
    def test_unset_or_empty_settings_take_their_defaults(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        import importlib

        import agentibridge.config as config

        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
        monkeypatch.setattr("shutil.which", lambda name: None)
        for key in ("AGENTIBRIDGE_MASTER_DIR", "AGENTIHOOKS_BINARY"):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        importlib.reload(config)

        assert config.AGENTIBRIDGE_MASTER_DIR == str(Path.home() / "dev" / "tcc-ecosystem")
        assert config.AGENTIHOOKS_BINARY == str(Path.home() / ".local" / "bin" / "agentihooks")
        monkeypatch.undo()
        importlib.reload(config)
