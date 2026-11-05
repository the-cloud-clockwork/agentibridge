"""Unit tests for run_command: bash and PowerShell runs on the bridge host."""

import shutil
import time
from pathlib import Path
from typing import Any

import pytest

import agentibridge.commands as commands

HOST = {
    "os": "Linux",
    "distribution": "Distro 1.0",
    "kernel": "6.0-test",
    "arch": "x86_64",
    "wsl": True,
    "hostname": "box",
    "user": "someone",
    "shells": {"bash": "/bin/bash", "powershell": "/usr/bin/pwsh"},
    "default_shell": "bash",
}


def _run(command: str, **options: Any) -> dict[str, Any]:
    return commands.run_command(command, **options)


def _alive(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    try:
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, IndexError):
        return False


@pytest.mark.unit
class TestPreflight:
    def test_reports_the_host_and_a_working_bash(self) -> None:
        host = commands.preflight()

        assert host["os"] in ("Linux", "Darwin", "Windows")
        assert host["default_shell"] in host["shells"] or host["os"] == "Windows"
        assert host["shells"].get("bash") == shutil.which("bash")

    def test_description_carries_the_preflight(self) -> None:
        text = commands.describe(HOST)

        assert "Host: Distro 1.0 (WSL), Linux 6.0-test, x86_64, user someone on box." in text
        assert "Shells: bash, powershell. Default: bash." in text
        assert "Commands run as someone and can read every file that user can" in text

    @pytest.mark.parametrize(
        ("env", "user"),
        [({"USER": "alice"}, "alice"), ({"USERNAME": "bob"}, "bob"), ({}, "unknown")],
        ids=["user", "username", "neither"],
    )
    def test_user_comes_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], user: str
    ) -> None:
        monkeypatch.delenv("USER", raising=False)
        monkeypatch.delenv("USERNAME", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        commands.preflight.cache_clear()

        try:
            assert commands.preflight()["user"] == user
        finally:
            commands.preflight.cache_clear()


@pytest.mark.unit
class TestRunCommand:
    def test_returns_exit_code_and_output(self) -> None:
        result = _run("echo out; echo err >&2; exit 3")

        assert result["exit_code"] == 3
        assert result["success"] is False
        assert result["stdout"] == "out\n"
        assert result["stderr"] == "err\n"
        assert result["timed_out"] is False
        assert result["shell"] == "bash"

    def test_hanging_command_is_killed_at_the_timeout(self) -> None:
        started = time.monotonic()

        result = _run("echo before; sleep 30 & sleep 30; echo never", timeout=1)

        assert result["timed_out"] is True
        assert result["success"] is False
        assert result["stdout"] == "before\n"
        assert time.monotonic() - started < 10

    def test_stdin_is_closed(self) -> None:
        result = _run("cat; echo done", timeout=5)

        assert result["stdout"] == "done\n"
        assert result["timed_out"] is False

    def test_output_is_capped(self) -> None:
        result = _run(f"head -c {commands.OUTPUT_LIMIT * 2} /dev/zero | tr '\\0' a")

        assert len(result["stdout"]) == commands.OUTPUT_LIMIT
        assert result["truncated"] is True

    def test_a_detached_child_holding_the_pipe_does_not_hang_the_call(self) -> None:
        started = time.monotonic()

        result = _run("(setsid sleep 30 &); echo done", timeout=20)

        assert result["stdout"] == "done\n"
        assert time.monotonic() - started < 10

    def test_a_background_child_dies_when_the_command_exits(self) -> None:
        result = _run("sleep 300 & echo $!", timeout=20)
        pid = int(result["stdout"])
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        assert result["exit_code"] == 0
        assert not _alive(pid)

    def test_bridge_environment_stays_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTIBRIDGE_API_KEYS", "visible-if-leaked")

        result = _run('echo "${AGENTIBRIDGE_API_KEYS:-absent} $TERM"')

        assert result["stdout"] == "absent dumb\n"

    def test_runs_in_the_requested_directory(self, tmp_path: Any) -> None:
        assert _run("pwd", cwd=str(tmp_path))["stdout"] == f"{tmp_path}\n"

    def test_missing_directory_is_an_error(self, tmp_path: Any) -> None:
        result = _run("pwd", cwd=str(tmp_path / "absent"))

        assert result["success"] is False
        assert "no such directory" in result["error"]

    def test_unknown_shell_names_the_available_ones(self) -> None:
        result = _run("echo hi", shell="fish")

        assert result["success"] is False
        assert "available: " in result["error"]
        assert "bash" in result["error"]

    @pytest.mark.skipif(not shutil.which("pwsh"), reason="PowerShell is not installed")
    def test_powershell_runs_without_a_profile(self) -> None:
        result = _run("Write-Output ('a' + 'b')", shell="powershell")

        assert result["stdout"].strip() == "ab"
        assert result["exit_code"] == 0
