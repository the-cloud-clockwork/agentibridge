"""Tests for agentibridge.daemon lifecycle management."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agentibridge import daemon


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Isolate all daemon state files under tmp_path."""
    monkeypatch.setattr(daemon, "STATE_DIR", tmp_path)
    monkeypatch.setattr(daemon, "LOCK_FILE", tmp_path / "mcp-daemon.lock")
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "mcp-daemon.pid")
    monkeypatch.setattr(daemon, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "logs" / "mcp-daemon.log")
    return tmp_path


@pytest.mark.unit
class TestSupervisor:
    def test_forced_by_env(self, monkeypatch):
        monkeypatch.setenv("AGENTIBRIDGE_MCP_SUPERVISOR", "pidfile")
        assert daemon.supervisor() == "pidfile"
        monkeypatch.setenv("AGENTIBRIDGE_MCP_SUPERVISOR", "systemd")
        assert daemon.supervisor() == "systemd"

    def test_no_systemctl_binary_means_pidfile(self, monkeypatch):
        monkeypatch.delenv("AGENTIBRIDGE_MCP_SUPERVISOR", raising=False)
        with patch("agentibridge.daemon.subprocess.run", side_effect=FileNotFoundError):
            assert daemon.supervisor() == "pidfile"

    def test_no_user_bus_means_pidfile(self, monkeypatch):
        """show-environment rc!=0 = no user bus at all (is-active can't
        distinguish that from a merely stopped unit)."""
        monkeypatch.delenv("AGENTIBRIDGE_MCP_SUPERVISOR", raising=False)
        result = MagicMock(returncode=1)
        with patch("agentibridge.daemon.subprocess.run", return_value=result) as mock_run:
            assert daemon.supervisor() == "pidfile"
        assert mock_run.call_args[0][0] == ["systemctl", "--user", "show-environment"]

    def test_user_bus_means_systemd(self, monkeypatch):
        monkeypatch.delenv("AGENTIBRIDGE_MCP_SUPERVISOR", raising=False)
        with patch("agentibridge.daemon.subprocess.run", return_value=MagicMock(returncode=0)):
            assert daemon.supervisor() == "systemd"


@pytest.mark.unit
class TestPidAlive:
    def test_dead_pid(self):
        proc = subprocess.Popen(["sleep", "0.01"])
        proc.wait()
        assert daemon.pid_alive(proc.pid) is False

    def test_recycled_pid_rejected_by_cmdline_marker(self):
        """A live pid whose argv lacks the serve/--sse pair must NOT pass —
        that is the recycled-pid guard."""
        proc = subprocess.Popen(["sleep", "5"])
        try:
            assert daemon.pid_alive(proc.pid) is False
        finally:
            proc.kill()
            proc.wait()

    def test_marker_match(self, monkeypatch):
        proc = subprocess.Popen(["sleep", "5"])
        try:
            monkeypatch.setattr(
                daemon, "_read_cmdline", lambda pid: ["python", "-m", "agentibridge.cli", "serve", "--sse"]
            )
            assert daemon.pid_alive(proc.pid) is True
        finally:
            proc.kill()
            proc.wait()


@pytest.mark.unit
class TestStop:
    def test_tears_down_both_backends(self, state_dir):
        """stop() must hit systemd AND the pidfile child, whichever exists."""
        daemon.PID_FILE.write_text("999999999\n")
        with patch("agentibridge.daemon.subprocess.run") as mock_run:
            daemon.stop()

        assert ["systemctl", "--user", "stop", "agentibridge"] in [c[0][0] for c in mock_run.call_args_list]
        assert not daemon.PID_FILE.exists()

    def test_sigterm_then_sigkill(self, state_dir, monkeypatch):
        monkeypatch.setattr(daemon, "_STOP_POLL_SECONDS", 0.2)
        proc = subprocess.Popen(["sleep", "30"])
        daemon.PID_FILE.write_text(f"{proc.pid}\n")
        monkeypatch.setattr(
            daemon, "_read_cmdline", lambda pid: ["agentibridge", "serve", "--sse"] if pid == proc.pid else []
        )
        with patch("agentibridge.daemon.subprocess.run"):
            daemon.stop()
        proc.wait(timeout=5)
        assert not daemon.PID_FILE.exists()


@pytest.mark.unit
class TestEnsureRunning:
    def test_restart_stops_first_then_spawns_pidfile(self, state_dir, monkeypatch):
        monkeypatch.setenv("AGENTIBRIDGE_MCP_SUPERVISOR", "pidfile")
        order = []
        monkeypatch.setattr(daemon, "stop", lambda: order.append("stop"))
        monkeypatch.setattr(daemon, "_start_databases", lambda sd: order.append("db"))
        fake_proc = MagicMock(pid=4242)
        monkeypatch.setattr(daemon, "_spawn_pidfile_child", lambda ef: (order.append("spawn"), fake_proc)[1])
        monkeypatch.setattr(daemon, "pid_alive", lambda pid: pid == 4242)
        monkeypatch.setattr(daemon, "port_open", lambda port: True)

        ok = daemon.ensure_running(restart=True, env_file=state_dir / "e.env", stack_dir=state_dir)

        assert ok is True
        assert order == ["stop", "db", "spawn"]
        assert daemon.PID_FILE.read_text().strip() == "4242"

    def test_no_restart_returns_early_when_alive(self, state_dir, monkeypatch):
        daemon.PID_FILE.write_text("777\n")
        monkeypatch.setattr(daemon, "pid_alive", lambda pid: pid == 777)
        spawned = []
        monkeypatch.setattr(daemon, "_spawn_pidfile_child", lambda ef: spawned.append(ef))

        assert daemon.ensure_running() is True
        assert spawned == []

    def test_child_death_during_startup_fails(self, state_dir, monkeypatch):
        """Liveness before port: a dead child fails even if some other
        process answers the port."""
        monkeypatch.setenv("AGENTIBRIDGE_MCP_SUPERVISOR", "pidfile")
        monkeypatch.setattr(daemon, "_START_POLL_SECONDS", 0.5)
        monkeypatch.setattr(daemon, "stop", lambda: None)
        monkeypatch.setattr(daemon, "_start_databases", lambda sd: None)
        monkeypatch.setattr(daemon, "_spawn_pidfile_child", lambda ef: MagicMock(pid=4242))
        monkeypatch.setattr(daemon, "pid_alive", lambda pid: False)
        monkeypatch.setattr(daemon, "port_open", lambda port: True)

        assert daemon.ensure_running(restart=True, env_file=state_dir / "e.env", stack_dir=state_dir) is False

    def test_systemd_backend_success_and_failed_unit(self, state_dir, monkeypatch):
        """The systemd branch of the poll loop: port open = success; a unit
        in terminal failed state exits early instead of burning the window."""
        monkeypatch.setenv("AGENTIBRIDGE_MCP_SUPERVISOR", "systemd")
        monkeypatch.setattr(daemon, "stop", lambda: None)
        monkeypatch.setattr(daemon, "_start_systemd", lambda: None)
        monkeypatch.setattr(daemon, "_systemd_unit_failed", lambda: False)
        monkeypatch.setattr(daemon, "port_open", lambda port: True)
        assert daemon.ensure_running(restart=True, env_file=state_dir / "e.env", stack_dir=state_dir) is True

        monkeypatch.setattr(daemon, "_START_POLL_SECONDS", 5.0)
        monkeypatch.setattr(daemon, "_systemd_unit_failed", lambda: True)
        monkeypatch.setattr(daemon, "port_open", lambda port: False)
        import time as _time

        started = _time.monotonic()
        assert daemon.ensure_running(restart=True, env_file=state_dir / "e.env", stack_dir=state_dir) is False
        assert _time.monotonic() - started < 2.0  # early exit, not the full window

    def test_concurrent_starts_spawn_once(self, state_dir, monkeypatch):
        """Decide+spawn share one flock: two overlapping starts must yield
        exactly one child — the loser sees the winner's live pid and returns."""
        import threading

        monkeypatch.setenv("AGENTIBRIDGE_MCP_SUPERVISOR", "pidfile")
        monkeypatch.setattr(daemon, "_start_databases", lambda sd: None)
        monkeypatch.setattr(daemon, "port_open", lambda port: True)
        spawned = []
        live_pids = set()

        def slow_spawn(env_file):
            import time as _time

            _time.sleep(0.3)
            pid = 5000 + len(spawned)
            spawned.append(pid)
            live_pids.add(pid)
            return MagicMock(pid=pid)

        monkeypatch.setattr(daemon, "_spawn_pidfile_child", slow_spawn)
        monkeypatch.setattr(daemon, "pid_alive", lambda pid: pid in live_pids)

        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(daemon.ensure_running(env_file=state_dir / "e.env", stack_dir=state_dir))
            )
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results == [True, True]
        assert len(spawned) == 1

    def test_port_never_opens_fails(self, state_dir, monkeypatch):
        monkeypatch.setenv("AGENTIBRIDGE_MCP_SUPERVISOR", "pidfile")
        monkeypatch.setattr(daemon, "_START_POLL_SECONDS", 0.4)
        monkeypatch.setattr(daemon, "stop", lambda: None)
        monkeypatch.setattr(daemon, "_start_databases", lambda sd: None)
        monkeypatch.setattr(daemon, "_spawn_pidfile_child", lambda ef: MagicMock(pid=4242))
        monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
        monkeypatch.setattr(daemon, "port_open", lambda port: False)

        assert daemon.ensure_running(restart=True, env_file=state_dir / "e.env", stack_dir=state_dir) is False


@pytest.mark.unit
class TestSpawnEnv:
    def test_transport_not_inherited(self, state_dir, monkeypatch):
        """A stray AGENTIBRIDGE_TRANSPORT export must not reach the child —
        the --sse flag is the only transport authority."""
        monkeypatch.setenv("AGENTIBRIDGE_TRANSPORT", "stdio")
        monkeypatch.setenv("AGENTIBRIDGE_PORT", "9999")
        monkeypatch.setenv("AGENTIBRIDGE_HOST", "0.0.0.0")
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs["env"]
            captured["start_new_session"] = kwargs["start_new_session"]
            return MagicMock(pid=1)

        with patch("agentibridge.daemon.subprocess.Popen", side_effect=fake_popen):
            daemon._spawn_pidfile_child(state_dir / "e.env")

        assert "AGENTIBRIDGE_TRANSPORT" not in captured["env"]
        # port/host must come from the env file the child loads, never the
        # invoking shell — load_dotenv(override=False) would let a stray
        # export win and the child would serve a port nothing registered.
        assert "AGENTIBRIDGE_PORT" not in captured["env"]
        assert "AGENTIBRIDGE_HOST" not in captured["env"]
        assert captured["env"]["AGENTIBRIDGE_ENV_FILE"] == str(state_dir / "e.env")
        assert captured["cmd"][-2:] == ["serve", "--sse"]
        assert captured["start_new_session"] is True


@pytest.mark.unit
class TestDaemonPort:
    def test_reads_env_file(self, state_dir):
        (state_dir / "agentibridge.env").write_text("AGENTIBRIDGE_PORT=9300\n")
        assert daemon.daemon_port() == 9300

    def test_default(self, state_dir, monkeypatch):
        monkeypatch.delenv("AGENTIBRIDGE_PORT", raising=False)
        assert daemon.daemon_port() == 8100
