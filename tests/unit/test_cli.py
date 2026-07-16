"""Tests for agentibridge.cli module."""

import re
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agentibridge import __version__
from agentibridge.cli import (
    main,
    cmd_version,
    cmd_help,
    cmd_connect,
    cmd_config,
    cmd_status,
    cmd_install,
    cmd_uninstall,
    cmd_stop,
    cmd_restart,
    cmd_logs,
    cmd_tunnel,
    cmd_update,
    cmd_embeddings,
    _container_health,
    _systemd_active,
    _launchd_state,
    _build_launchd_plist,
    _build_launchd_db_plist,
    _launchd_bootstrap,
    _launchd_bootout,
    _LAUNCHD_LABEL,
    _LAUNCHD_DB_LABEL,
    _cloudflared_hostname,
    _parse_cloudflared_config,
    _extract_tunnel_url,
    _short_digest,
    _validate_env,
    _ensure_stack_dir,
    _read_env_value,
)


@pytest.mark.unit
class TestCmdVersion:
    def test_prints_version(self, capsys):
        args = MagicMock()
        cmd_version(args)
        output = capsys.readouterr().out
        assert "agentibridge" in output
        assert re.search(r"\d+\.\d+\.\d+", output)


@pytest.mark.unit
class TestCmdHelp:
    def test_shows_tools(self, capsys):
        args = MagicMock()
        cmd_help(args)
        output = capsys.readouterr().out
        assert "list_sessions" in output
        assert "get_session" in output
        assert "search_semantic" in output
        assert "dispatch_task" in output
        assert "CONFIGURATION" in output

    def test_shows_env_vars(self, capsys):
        args = MagicMock()
        cmd_help(args)
        output = capsys.readouterr().out
        assert "REDIS_URL" in output
        assert "AGENTIBRIDGE_TRANSPORT" in output
        assert "LLM_API_BASE" in output
        assert "CLOUDFLARE_TUNNEL_TOKEN" in output


@pytest.mark.unit
class TestCmdConnect:
    def test_default_connection_strings(self, capsys):
        args = MagicMock()
        args.host = None
        args.port = None
        args.api_key = None
        import os

        env = os.environ.copy()
        env.pop("AGENTIBRIDGE_HOST", None)
        env.pop("AGENTIBRIDGE_PORT", None)
        with patch.dict("os.environ", env, clear=True):
            cmd_connect(args)
        output = capsys.readouterr().out
        assert "Claude Code CLI" in output
        assert "ChatGPT" in output
        assert "localhost:8100" in output
        assert '"type": "http"' in output
        assert "/sse" in output
        assert "/health" in output

    def test_custom_host_port(self, capsys):
        args = MagicMock()
        args.host = "myserver.com"
        args.port = "9000"
        args.api_key = "secret-key"
        cmd_connect(args)
        output = capsys.readouterr().out
        assert "myserver.com:9000" in output
        assert "secret-key" in output


@pytest.mark.unit
class TestCmdConfig:
    def test_shows_config(self, capsys):
        args = MagicMock()
        args.generate_env = False
        cmd_config(args)
        output = capsys.readouterr().out
        assert "AGENTIBRIDGE_TRANSPORT" in output
        assert "AGENTIBRIDGE_PORT" in output

    def test_generate_env(self, capsys):
        args = MagicMock()
        args.generate_env = True
        cmd_config(args)
        output = capsys.readouterr().out
        assert "AGENTIBRIDGE_TRANSPORT" in output
        assert "REDIS_URL" in output
        assert "LLM_API_BASE" in output


@pytest.mark.unit
class TestCmdStatus:
    """Tests for `agentibridge status`."""

    def _docker_inspect_side_effect(self, container_health: dict):
        """Return a side_effect that handles docker inspect + systemctl calls."""

        def side_effect(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            # docker inspect for container health
            if "docker" in cmd_str and "inspect" in cmd_str:
                for name, status in container_health.items():
                    if name in cmd_str:
                        if status is None:
                            return _fail()
                        return _ok(stdout=status)
                return _fail()
            # systemctl --user is-active agentibridge
            if "systemctl" in cmd_str and "--user" in cmd_str:
                return _ok(stdout="inactive")
            # systemctl is-active cloudflared
            if "systemctl" in cmd_str and "cloudflared" in cmd_str:
                return _ok(stdout="inactive")
            return _fail()

        return side_effect

    def test_status_with_env_file(self, capsys, tmp_path):
        """Reads config from agentibridge.env and shows container health."""
        stack_dir = tmp_path / "agentibridge"
        stack_dir.mkdir()
        env_file = stack_dir / "agentibridge.env"
        env_file.write_text(
            "REDIS_URL=redis://localhost:6379/0\n"
            "POSTGRES_URL=postgresql://ab:secret@localhost:5432/agentibridge\n"
            "AGENTIBRIDGE_TRANSPORT=sse\n"
            "AGENTIBRIDGE_PORT=8100\n"
            "AGENTIBRIDGE_POLL_INTERVAL=30\n"
        )

        container_health = {
            "agentibridge-redis": "running",
            "agentibridge-postgres": "healthy",
            "agentibridge-tunnel": None,
        }

        se = self._docker_inspect_side_effect(container_health)

        mock_get_redis = MagicMock(return_value=None)
        mock_get_pg = MagicMock(return_value=None)

        with (
            patch("agentibridge.cli._STACK_DIR", stack_dir),
            patch("agentibridge.cli.subprocess.run", side_effect=se),
            patch.dict("sys.modules", {"agentibridge.redis_client": MagicMock(get_redis=mock_get_redis)}),
            patch.dict("sys.modules", {"agentibridge.pg_client": MagicMock(get_pg=mock_get_pg)}),
        ):
            cmd_status(MagicMock())

        output = capsys.readouterr().out

        assert "transport: sse" in output
        assert "port: 8100" in output
        assert "poll interval: 30s" in output
        assert "env file:" in output

    def test_status_no_env_file(self, capsys, tmp_path):
        """Falls back to env vars when agentibridge.env doesn't exist."""
        stack_dir = tmp_path / "agentibridge"
        stack_dir.mkdir()

        def se(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "systemctl" in cmd_str and "--user" in cmd_str:
                return _ok(stdout="inactive")
            if "systemctl" in cmd_str and "cloudflared" in cmd_str:
                return _ok(stdout="inactive")
            return _fail()

        mock_get_redis = MagicMock(return_value=None)
        mock_get_pg = MagicMock(return_value=None)

        with (
            patch("agentibridge.cli._STACK_DIR", stack_dir),
            patch("agentibridge.cli.subprocess.run", side_effect=se),
            patch.dict("sys.modules", {"agentibridge.redis_client": MagicMock(get_redis=mock_get_redis)}),
            patch.dict("sys.modules", {"agentibridge.pg_client": MagicMock(get_pg=mock_get_pg)}),
            patch.dict("os.environ", {"AGENTIBRIDGE_TRANSPORT": "sse", "AGENTIBRIDGE_PORT": "8100"}),
        ):
            cmd_status(MagicMock())

        output = capsys.readouterr().out
        assert "transport: sse" in output
        assert "unavailable" in output

    def test_status_tunnel_systemd(self, capsys):
        """When cloudflared systemd service is active, shows 'systemd' in output."""

        def se(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "systemctl" in cmd_str and "cloudflared" in cmd_str:
                return _ok(stdout="active")
            if "systemctl" in cmd_str and "--user" in cmd_str:
                return _ok(stdout="inactive")
            return _fail()

        with (
            patch("agentibridge.cli.subprocess.run", side_effect=se),
            patch("agentibridge.cli._cloudflared_hostname", return_value="tunnel.example.com"),
        ):
            mock_get_redis = MagicMock(return_value=None)
            mock_get_pg = MagicMock(return_value=None)
            with (
                patch.dict("sys.modules", {"agentibridge.redis_client": MagicMock(get_redis=mock_get_redis)}),
                patch.dict("sys.modules", {"agentibridge.pg_client": MagicMock(get_pg=mock_get_pg)}),
            ):
                cmd_status(MagicMock())

        output = capsys.readouterr().out
        assert "active (systemd)" in output
        assert "hostname: tunnel.example.com" in output


@pytest.mark.unit
class TestContainerHealth:
    def test_returns_health_status(self):
        with patch("agentibridge.cli.subprocess.run", return_value=_ok(stdout="healthy")):
            assert _container_health("agentibridge-redis") == "healthy"

    def test_returns_none_when_not_found(self):
        with patch("agentibridge.cli.subprocess.run", return_value=_fail()):
            assert _container_health("agentibridge-redis") is None

    def test_returns_none_on_exception(self):
        with patch("agentibridge.cli.subprocess.run", side_effect=Exception("no docker")):
            assert _container_health("agentibridge-redis") is None


@pytest.mark.unit
class TestSystemdActive:
    def test_returns_active(self):
        with patch("agentibridge.cli.subprocess.run", return_value=_ok(stdout="active")):
            assert _systemd_active("cloudflared") == "active"

    def test_returns_inactive(self):
        with patch("agentibridge.cli.subprocess.run", return_value=_ok(stdout="inactive")):
            assert _systemd_active("cloudflared") == "inactive"

    def test_returns_none_on_exception(self):
        with patch("agentibridge.cli.subprocess.run", side_effect=FileNotFoundError):
            assert _systemd_active("cloudflared") is None


@pytest.mark.unit
class TestLaunchdState:
    """Tests for _launchd_state() — the Darwin analogue of _systemd_active()."""

    def test_returns_running(self):
        stdout = "gui/501/com.agentibridge = {\n\tstate = running\n}\n"
        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli.os.getuid", return_value=501),
            patch("agentibridge.cli.subprocess.run", return_value=_ok(stdout=stdout)),
        ):
            assert _launchd_state("com.agentibridge") == "running"

    def test_returns_not_loaded_on_nonzero_exit(self):
        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli.os.getuid", return_value=501),
            patch("agentibridge.cli.subprocess.run", return_value=_fail()),
        ):
            assert _launchd_state("com.agentibridge") == "not loaded"

    def test_returns_loaded_when_no_state_line(self):
        stdout = "gui/501/com.agentibridge = {\n\tpath = /Library/LaunchAgents/com.agentibridge.plist\n}\n"
        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli.os.getuid", return_value=501),
            patch("agentibridge.cli.subprocess.run", return_value=_ok(stdout=stdout)),
        ):
            assert _launchd_state("com.agentibridge") == "loaded"

    def test_returns_none_on_exception(self):
        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli.os.getuid", return_value=501),
            patch("agentibridge.cli.subprocess.run", side_effect=FileNotFoundError),
        ):
            assert _launchd_state("com.agentibridge") is None

    def test_returns_none_on_non_darwin(self):
        with patch("agentibridge.cli.platform.system", return_value="Linux"):
            assert _launchd_state("com.agentibridge") is None


@pytest.mark.unit
class TestBuildLaunchdPlist:
    """Tests for _build_launchd_plist() — systemd EnvironmentFile= parity."""

    def test_no_env_file(self, tmp_path):
        plist = _build_launchd_plist(tmp_path / "missing.env", "/usr/bin/python3")
        assert plist["EnvironmentVariables"] == {"AGENTIBRIDGE_TRANSPORT": "sse"}
        assert plist["Label"] == _LAUNCHD_LABEL
        assert plist["ProgramArguments"] == ["/usr/bin/python3", "-m", "agentibridge"]

    def test_strips_export_prefix(self, tmp_path):
        env_file = tmp_path / "agentibridge.env"
        env_file.write_text("export FOO=bar\n")
        with patch("agentibridge.cli._LAUNCHD_LOG_DIR", tmp_path):
            plist = _build_launchd_plist(env_file, "/usr/bin/python3")
        assert plist["EnvironmentVariables"]["FOO"] == "bar"
        assert "export FOO" not in plist["EnvironmentVariables"]

    def test_skips_hash_and_semicolon_comments(self, tmp_path):
        env_file = tmp_path / "agentibridge.env"
        env_file.write_text("# hash comment\n; semicolon comment\nFOO=bar\n")
        with patch("agentibridge.cli._LAUNCHD_LOG_DIR", tmp_path):
            plist = _build_launchd_plist(env_file, "/usr/bin/python3")
        assert plist["EnvironmentVariables"] == {"AGENTIBRIDGE_TRANSPORT": "sse", "FOO": "bar"}

    def test_strips_quoted_values(self, tmp_path):
        env_file = tmp_path / "agentibridge.env"
        env_file.write_text('FOO="bar"\nBAZ=\'qux\'\n')
        with patch("agentibridge.cli._LAUNCHD_LOG_DIR", tmp_path):
            plist = _build_launchd_plist(env_file, "/usr/bin/python3")
        assert plist["EnvironmentVariables"]["FOO"] == "bar"
        assert plist["EnvironmentVariables"]["BAZ"] == "qux"

    def test_export_and_comment_combined(self, tmp_path):
        env_file = tmp_path / "agentibridge.env"
        env_file.write_text("export KEY=VAL\n; ignored\n# also ignored\nexport OTHER=1\n")
        with patch("agentibridge.cli._LAUNCHD_LOG_DIR", tmp_path):
            plist = _build_launchd_plist(env_file, "/usr/bin/python3")
        assert plist["EnvironmentVariables"]["KEY"] == "VAL"
        assert plist["EnvironmentVariables"]["OTHER"] == "1"
        assert len(plist["EnvironmentVariables"]) == 3  # + AGENTIBRIDGE_TRANSPORT default


@pytest.mark.unit
class TestBuildLaunchdDbPlist:
    """Tests for _build_launchd_db_plist() — the DB-stack login/reboot agent."""

    def test_program_arguments(self, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        with (
            patch("agentibridge.cli._LAUNCHD_LOG_DIR", tmp_path),
            patch("shutil.which", return_value="/usr/local/bin/docker"),
        ):
            plist = _build_launchd_db_plist(compose_file)
        assert plist["Label"] == _LAUNCHD_DB_LABEL
        assert plist["ProgramArguments"] == [
            "/usr/local/bin/docker",
            "compose",
            "-f",
            str(compose_file),
            "up",
            "-d",
        ]
        assert plist["RunAtLoad"] is True
        assert plist["KeepAlive"] is False

    def test_falls_back_to_bare_docker_when_not_on_path(self, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        with (
            patch("agentibridge.cli._LAUNCHD_LOG_DIR", tmp_path),
            patch("shutil.which", return_value=None),
        ):
            plist = _build_launchd_db_plist(compose_file)
        assert plist["ProgramArguments"][0] == "docker"


@pytest.mark.unit
class TestLaunchdBootstrap:
    def test_bootstrap_success(self):
        with patch("agentibridge.cli.subprocess.run", return_value=_ok()) as mock_run:
            assert _launchd_bootstrap(Path("/tmp/com.agentibridge.plist")) is True
        assert mock_run.call_count == 1
        assert mock_run.call_args[0][0][:2] == ["launchctl", "bootstrap"]

    def test_falls_back_to_load_w_on_bootstrap_failure(self):
        with patch(
            "agentibridge.cli.subprocess.run",
            side_effect=[_fail(), _ok()],
        ) as mock_run:
            assert _launchd_bootstrap(Path("/tmp/com.agentibridge.plist")) is True
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1][0][0][:2] == ["launchctl", "load"]

    def test_returns_false_when_both_fail(self):
        with patch("agentibridge.cli.subprocess.run", side_effect=[_fail(), _fail()]):
            assert _launchd_bootstrap(Path("/tmp/com.agentibridge.plist")) is False


@pytest.mark.unit
class TestLaunchdBootout:
    def test_calls_launchctl_bootout(self):
        with (
            patch("agentibridge.cli.os.getuid", return_value=501),
            patch("agentibridge.cli.subprocess.run") as mock_run,
        ):
            _launchd_bootout("com.agentibridge")
        mock_run.assert_called_once_with(
            ["launchctl", "bootout", "gui/501/com.agentibridge"],
            capture_output=True,
            check=False,
        )

    def test_does_not_raise_when_not_loaded(self):
        with (
            patch("agentibridge.cli.os.getuid", return_value=501),
            patch("agentibridge.cli.subprocess.run", return_value=_fail()),
        ):
            _launchd_bootout("com.agentibridge")  # best-effort, no exception


def _mock_cloudflared_dir(tmp_path):
    """Create a .cloudflared dir under tmp_path and patch the constants."""
    cf_dir = tmp_path / ".cloudflared"
    cf_dir.mkdir()
    return cf_dir


@pytest.mark.unit
class TestCloudflaredHostname:
    def test_extracts_hostname(self, tmp_path):
        cf_dir = _mock_cloudflared_dir(tmp_path)
        (cf_dir / "config.yml").write_text(
            "tunnel: abc-123\ningress:\n  - hostname: bridge.example.com\n    service: http://localhost:8100\n"
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _cloudflared_hostname() == "bridge.example.com"

    def test_returns_none_when_no_file(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _cloudflared_hostname() is None


@pytest.mark.unit
class TestParseCloudflaredConfig:
    def test_parses_full_config(self, tmp_path):
        cf_dir = _mock_cloudflared_dir(tmp_path)
        (cf_dir / "config.yml").write_text(
            "tunnel: abc-123\n"
            "credentials-file: /home/user/.cloudflared/abc-123.json\n"
            "ingress:\n"
            "  - hostname: bridge.example.com\n"
            "    service: http://localhost:8100\n"
            "  - service: http_status:404\n"
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            info = _parse_cloudflared_config()
        assert info["tunnel_id"] == "abc-123"
        assert info["hostname"] == "bridge.example.com"
        assert info["service"] == "http://localhost:8100"
        assert info["credentials_file"] == "/home/user/.cloudflared/abc-123.json"

    def test_returns_empty_when_no_file(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _parse_cloudflared_config() == {}


@pytest.mark.unit
class TestMain:
    def test_no_args_prints_help(self, capsys):
        with patch("sys.argv", ["agentibridge"]):
            main()
        output = capsys.readouterr().out
        assert "agentibridge" in output.lower() or "usage" in output.lower()

    def test_version_command(self, capsys):
        with patch("sys.argv", ["agentibridge", "version"]):
            main()
        output = capsys.readouterr().out
        assert re.search(r"\d+\.\d+\.\d+", output)


@pytest.mark.unit
class TestExtractTunnelUrl:
    def test_extracts_quick_tunnel_url(self):
        logs = (
            "2024-01-01 INFO Starting quick tunnel...\n"
            "2024-01-01 INFO +----------------------------+\n"
            "2024-01-01 INFO | https://foo-bar-baz.trycloudflare.com |\n"
            "2024-01-01 INFO +----------------------------+\n"
        )
        assert _extract_tunnel_url(logs) == "https://foo-bar-baz.trycloudflare.com"

    def test_returns_none_for_no_url(self):
        assert _extract_tunnel_url("Starting named tunnel...\nConnected.") is None

    def test_returns_none_for_empty(self):
        assert _extract_tunnel_url("") is None


@pytest.mark.unit
class TestCmdTunnel:
    def test_tunnel_no_docker_no_systemd(self, capsys):
        with (
            patch("shutil.which", return_value=None),
            patch("agentibridge.cli._systemd_active", return_value="inactive"),
        ):
            args = MagicMock()
            cmd_tunnel(args)
        output = capsys.readouterr().out
        assert "not running" in output

    def test_tunnel_not_running_no_systemd(self, capsys):
        with patch("shutil.which", return_value="/usr/bin/docker"):
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            with (
                patch("agentibridge.cli.subprocess.run", return_value=mock_result),
                patch("agentibridge.cli._systemd_active", return_value="inactive"),
            ):
                args = MagicMock()
                cmd_tunnel(args)
        output = capsys.readouterr().out
        assert "not running" in output
        assert "docker compose --profile tunnel up -d" in output

    def test_tunnel_quick_url_detected(self, capsys):
        inspect_result = MagicMock()
        inspect_result.returncode = 0
        inspect_result.stdout = "running"

        log_result = MagicMock()
        log_result.returncode = 0
        log_result.stdout = ""
        log_result.stderr = (
            "INF +-------------------------------------------+\n"
            "INF | https://my-test-tunnel.trycloudflare.com  |\n"
            "INF +-------------------------------------------+\n"
        )

        def side_effect(cmd, **kwargs):
            if "inspect" in cmd:
                return inspect_result
            return log_result

        with patch("shutil.which", return_value="/usr/bin/docker"):
            with patch("agentibridge.cli.subprocess.run", side_effect=side_effect):
                args = MagicMock()
                cmd_tunnel(args)
        output = capsys.readouterr().out
        assert "https://my-test-tunnel.trycloudflare.com" in output
        assert "quick tunnel" in output
        assert '"type": "http"' in output
        assert "/sse" in output

    def test_tunnel_named_connected(self, capsys):
        inspect_result = MagicMock()
        inspect_result.returncode = 0
        inspect_result.stdout = "running"

        log_result = MagicMock()
        log_result.returncode = 0
        log_result.stdout = "Starting named tunnel...\nConnection registered."
        log_result.stderr = ""

        def side_effect(cmd, **kwargs):
            if "inspect" in cmd:
                return inspect_result
            return log_result

        with patch("shutil.which", return_value="/usr/bin/docker"):
            with patch("agentibridge.cli.subprocess.run", side_effect=side_effect):
                args = MagicMock()
                cmd_tunnel(args)
        output = capsys.readouterr().out
        assert "named tunnel" in output

    def test_tunnel_systemd_with_config(self, capsys):
        """When no Docker tunnel but cloudflared runs via systemd, shows config."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        cfg_info = {
            "tunnel_id": "abc-123",
            "hostname": "bridge.example.com",
            "service": "http://localhost:8100",
            "credentials_file": "/home/user/.cloudflared/abc-123.json",
        }

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("agentibridge.cli.subprocess.run", return_value=mock_result),
            patch("agentibridge.cli._systemd_active", return_value="active"),
            patch("agentibridge.cli._parse_cloudflared_config", return_value=cfg_info),
        ):
            args = MagicMock()
            cmd_tunnel(args)

        output = capsys.readouterr().out
        assert "active (systemd)" in output
        assert "tunnel id: abc-123" in output
        assert "hostname:  bridge.example.com" in output
        assert "service:   http://localhost:8100" in output
        assert "https://bridge.example.com/health" in output
        assert "https://bridge.example.com/mcp" in output
        assert '"type": "http"' in output


@pytest.mark.unit
class TestShortDigest:
    def test_shortens_sha256(self):
        assert _short_digest("sha256:abcdef123456789") == "sha256:abcdef123456"

    def test_handles_none_digest(self):
        assert _short_digest("<none>") == "(none)"
        assert _short_digest("") == "(none)"

    def test_no_algo_prefix(self):
        assert _short_digest("abcdef1234567890") == "abcdef123456"


def _make_stack_dir() -> Path:
    """Return a temp Path with compose + agentibridge.env files for testing."""
    d = Path(tempfile.mkdtemp())
    (d / "docker-compose.yml").write_text("services: {}\n")
    (d / "agentibridge.env").write_text(
        "REDIS_URL=redis://r:6379/0\n"
        "POSTGRES_URL=postgresql://a:a@localhost/a\n"
        "POSTGRES_USER=a\nPOSTGRES_PASSWORD=a\nPOSTGRES_DB=a\n"
        "AGENTIBRIDGE_TRANSPORT=sse\nAGENTIBRIDGE_PORT=8100\n"
    )
    return d


def _ok(stdout="", stderr=""):
    """Helper: return a MagicMock subprocess result with rc=0."""
    r = MagicMock()
    r.returncode = 0
    r.stdout = stdout
    r.stderr = stderr
    return r


def _fail(stdout="", stderr=""):
    """Helper: return a MagicMock subprocess result with rc=1."""
    r = MagicMock()
    r.returncode = 1
    r.stdout = stdout
    r.stderr = stderr
    return r


@pytest.mark.unit
class TestCmdUpdate:
    """Tests for `agentibridge update`.

    Each test records every subprocess.run call so we can assert on the
    exact commands, their order, and that the right arguments are passed.
    """

    def _run_update(self, *, docker_flag=False, has_docker=False, side_effect=None):
        """Run cmd_update with mocks, return (calls, output).

        calls: list of (cmd_list, kwargs) tuples for every subprocess.run call.
        """
        calls = []
        original_side_effect = side_effect

        def recording_side_effect(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            return original_side_effect(cmd, **kwargs)

        args = MagicMock()
        args.docker = docker_flag

        docker_path = "/usr/bin/docker" if has_docker else None
        with patch("shutil.which", return_value=docker_path):
            with patch("agentibridge.cli.subprocess.run", side_effect=recording_side_effect):
                cmd_update(args)

        return calls

    # ── pip upgrade: correct command ──────────────────────────────────

    def test_calls_pip_install_upgrade(self, capsys):
        """Verifies the exact pip install --upgrade command."""
        import sys as _sys

        def se(cmd, **kw):
            cmd_str = " ".join(str(c) for c in cmd)
            if "pip" in cmd_str and "install" in cmd_str:
                return _ok()
            if "pip" in cmd_str and "show" in cmd_str:
                return _ok(stdout="Version: 0.3.0\n")
            return _fail()

        calls = self._run_update(side_effect=se)
        capsys.readouterr()  # consume output

        # First call should be pip install --upgrade agentibridge
        pip_cmd = calls[0][0]
        assert pip_cmd[0] == _sys.executable
        assert pip_cmd[1:] == ["-m", "pip", "install", "--upgrade", "agentibridge"]

        # Second call should be pip show agentibridge
        show_cmd = calls[1][0]
        assert show_cmd[0] == _sys.executable
        assert show_cmd[1:] == ["-m", "pip", "show", "agentibridge"]

        # pip install must capture output (not print pip noise)
        assert calls[0][1].get("capture_output") is True
        assert calls[0][1].get("text") is True

    # ── pip upgrade: version change detected ──────────────────────────

    def test_pip_version_change_reported(self, capsys):
        """Reports old -> new version when pip upgrade changes version."""
        # Use a version guaranteed to differ from the installed __version__
        new_version = "99.99.99"

        def se(cmd, **kw):
            cmd_str = " ".join(str(c) for c in cmd)
            if "pip" in cmd_str and "install" in cmd_str:
                return _ok()
            if "pip" in cmd_str and "show" in cmd_str:
                return _ok(stdout=f"Version: {new_version}\n")
            return _fail()

        self._run_update(side_effect=se)
        output = capsys.readouterr().out
        # Current version (from __version__) vs new version from pip show
        assert "Updated:" in output
        assert new_version in output

    # ── pip upgrade: already latest ───────────────────────────────────

    def test_pip_already_latest(self, capsys):
        """Shows 'already up to date' when pip show returns same version."""

        def se(cmd, **kw):
            cmd_str = " ".join(str(c) for c in cmd)
            if "pip" in cmd_str and "install" in cmd_str:
                return _ok()
            if "pip" in cmd_str and "show" in cmd_str:
                return _ok(stdout=f"Version: {__version__}\n")
            return _fail()

        self._run_update(side_effect=se)
        output = capsys.readouterr().out
        assert "Already up to date" in output

    # ── pip failure: exits with error ─────────────────────────────────

    def test_pip_failure_exits(self, capsys):
        """Exits with error when pip install fails (no --docker)."""

        def se(cmd, **kw):
            return _fail(stderr="Could not find a version")

        with pytest.raises(SystemExit):
            self._run_update(side_effect=se)

        output = capsys.readouterr().out
        assert "ERROR" in output

    # ── docker skipped when not installed ─────────────────────────────

    def test_no_docker_skips_docker(self, capsys):
        """When docker is not installed, no docker commands are run."""

        def se(cmd, **kw):
            cmd_str = " ".join(str(c) for c in cmd)
            if "pip" in cmd_str and "install" in cmd_str:
                return _ok()
            if "pip" in cmd_str and "show" in cmd_str:
                return _ok(stdout=f"Version: {__version__}\n")
            return _fail()

        calls = self._run_update(has_docker=False, side_effect=se)
        capsys.readouterr()

        # Only pip commands should have been called (no docker)
        for cmd, _ in calls:
            assert "docker" not in " ".join(cmd)

    # ── docker skipped when stack not running ─────────────────────────

    def test_docker_skipped_when_not_running(self, capsys):
        """When docker exists but stack is stopped, docker update is skipped."""

        def se(cmd, **kw):
            cmd_str = " ".join(str(c) for c in cmd)
            if "pip" in cmd_str and "install" in cmd_str:
                return _ok()
            if "pip" in cmd_str and "show" in cmd_str:
                return _ok(stdout=f"Version: {__version__}\n")
            if "inspect" in cmd_str:
                return _fail()  # container not found
            return _fail()

        calls = self._run_update(has_docker=True, side_effect=se)
        output = capsys.readouterr().out

        # Should mention it was skipped
        assert "skipped" in output.lower()

        # No docker compose pull should have been called
        for cmd, _ in calls:
            assert "pull" not in cmd

    # ── docker forced with --docker flag ──────────────────────────────

    def test_docker_forced_with_flag(self, capsys):
        """--docker flag forces docker update even when stack is stopped."""
        images_calls = {"n": 0}

        def se(cmd, **kw):
            cmd_str = " ".join(str(c) for c in cmd)
            if "pip" in cmd_str and "install" in cmd_str:
                return _ok()
            if "pip" in cmd_str and "show" in cmd_str:
                return _ok(stdout=f"Version: {__version__}\n")
            if "inspect" in cmd_str:
                return _fail()  # stack not running
            if "images" in cmd_str:
                images_calls["n"] += 1
                return _ok(stdout="sha256:aaa111222333")
            if "pull" in cmd_str:
                return _ok()
            if "ps" in cmd_str and "State" in cmd_str:
                return _ok(stdout="")  # stopped
            return _ok()

        with patch("agentibridge.cli._STACK_DIR", _make_stack_dir()):
            calls = self._run_update(docker_flag=True, has_docker=True, side_effect=se)

        capsys.readouterr()

        # docker compose pull agentibridge should have been called
        pull_calls = [cmd for cmd, _ in calls if "pull" in cmd]
        assert len(pull_calls) == 1
        assert "agentibridge" in pull_calls[0]

    # ── docker: pull + recreate when stack running ────────────────────

    def test_docker_pull_and_recreate_commands(self, capsys):
        """Verifies the exact docker compose commands for pull + recreate."""
        images_calls = {"n": 0}
        stack_dir = _make_stack_dir()

        def se(cmd, **kw):
            cmd_str = " ".join(str(c) for c in cmd)
            if "pip" in cmd_str and "install" in cmd_str:
                return _ok()
            if "pip" in cmd_str and "show" in cmd_str:
                return _ok(stdout=f"Version: {__version__}\n")
            if "inspect" in cmd_str:
                return _ok(stdout="running")
            if "images" in cmd_str:
                images_calls["n"] += 1
                if images_calls["n"] == 1:
                    return _ok(stdout="sha256:old000000000")
                return _ok(stdout="sha256:new111111111")
            if "pull" in cmd_str:
                return _ok()
            if "ps" in cmd_str and "State" in cmd_str:
                return _ok(stdout="running\nrunning\n")
            if "up" in cmd_str:
                return _ok()
            if "ps" in cmd_str:
                return _ok(stdout="agentibridge\tUp 1s\t8100/tcp")
            return _ok()

        with patch("agentibridge.cli._STACK_DIR", stack_dir):
            calls = self._run_update(has_docker=True, side_effect=se)

        output = capsys.readouterr().out

        # Verify pull command uses compose with correct file and env
        pull_calls = [cmd for cmd, _ in calls if "pull" in cmd]
        assert len(pull_calls) == 1
        pull_cmd = pull_calls[0]
        assert pull_cmd[:2] == ["docker", "compose"]
        assert "-f" in pull_cmd
        assert str(stack_dir / "docker-compose.yml") in pull_cmd
        assert "--env-file" in pull_cmd
        assert str(stack_dir / "agentibridge.env") in pull_cmd
        assert pull_cmd[-1] == "agentibridge"

        # Verify recreate command: up -d --no-deps --force-recreate agentibridge
        up_calls = [cmd for cmd, _ in calls if "up" in cmd]
        assert len(up_calls) == 1
        up_cmd = up_calls[0]
        assert "--no-deps" in up_cmd
        assert "--force-recreate" in up_cmd
        assert "-d" in up_cmd
        assert up_cmd[-1] == "agentibridge"

        # Verify digest comparison output
        assert "Image updated:" in output

    # ── docker: image already up to date ──────────────────────────────

    def test_docker_image_already_current(self, capsys):
        """When docker digest unchanged, reports 'already up to date'."""
        stack_dir = _make_stack_dir()

        def se(cmd, **kw):
            cmd_str = " ".join(str(c) for c in cmd)
            if "pip" in cmd_str and "install" in cmd_str:
                return _ok()
            if "pip" in cmd_str and "show" in cmd_str:
                return _ok(stdout=f"Version: {__version__}\n")
            if "inspect" in cmd_str:
                return _ok(stdout="running")
            if "images" in cmd_str:
                return _ok(stdout="sha256:same00000000")
            if "pull" in cmd_str:
                return _ok()
            if "ps" in cmd_str and "State" in cmd_str:
                return _ok(stdout="running\nrunning\n")
            if "up" in cmd_str:
                return _ok()
            if "ps" in cmd_str:
                return _ok(stdout="agentibridge\tUp 1s")
            return _ok()

        with patch("agentibridge.cli._STACK_DIR", stack_dir):
            self._run_update(has_docker=True, side_effect=se)

        output = capsys.readouterr().out
        assert "Image already up to date" in output


@pytest.mark.unit
class TestValidateEnv:
    def test_passes_when_all_vars_present(self, tmp_path):
        """No exit when all required vars are present."""
        env_file = tmp_path / "agentibridge.env"
        env_file.write_text(
            "REDIS_URL=redis://redis:6379/0\n"
            "POSTGRES_URL=postgresql://a:a@postgres:5432/a\n"
            "AGENTIBRIDGE_TRANSPORT=sse\n"
            "AGENTIBRIDGE_PORT=8100\n"
            "POSTGRES_USER=agentibridge\n"
            "POSTGRES_PASSWORD=agentibridge\n"
            "POSTGRES_DB=agentibridge\n"
        )
        _validate_env(env_file)  # should not raise

    def test_exits_when_vars_missing(self, tmp_path, capsys):
        """Exits with code 1 listing missing variables."""
        env_file = tmp_path / "agentibridge.env"
        env_file.write_text("REDIS_URL=redis://redis:6379/0\n")
        with pytest.raises(SystemExit) as exc:
            _validate_env(env_file)
        assert exc.value.code == 1
        output = capsys.readouterr().out
        assert "missing required variables" in output


@pytest.mark.unit
class TestEnsureStackDir:
    def test_scaffolds_compose_and_env(self, tmp_path, capsys):
        """Creates compose file and agentibridge.env, returns stack_dir on first run."""
        stack_dir = tmp_path / "agentibridge"
        with (
            patch("agentibridge.cli._STACK_DIR", stack_dir),
            patch("agentibridge.cli._LEGACY_STACK_DIR", tmp_path / "legacy"),
        ):
            result = _ensure_stack_dir()
        assert result == stack_dir
        output = capsys.readouterr().out
        assert "Created" in output
        assert "default configuration" in output
        assert "github.com" in output
        assert (stack_dir / "docker-compose.yml").exists()
        assert (stack_dir / "agentibridge.env").exists()

    def test_migrates_old_env_with_docker_vars(self, tmp_path, capsys):
        """Moves .env to agentibridge.env when it contains Docker vars."""
        stack_dir = tmp_path / "agentibridge"
        stack_dir.mkdir()
        # Write a compose file so it doesn't trigger first-run exit
        import shutil

        from agentibridge.cli import DATA_DIR

        shutil.copy2(DATA_DIR / "docker-compose.yml", stack_dir / "docker-compose.yml")
        # Create .env with Docker vars
        old_env = stack_dir / ".env"
        old_env.write_text(
            "REDIS_URL=redis://redis:6379/0\n"
            "POSTGRES_URL=postgresql://a:a@postgres:5432/a\n"
            "AGENTIBRIDGE_TRANSPORT=sse\n"
            "AGENTIBRIDGE_PORT=8100\n"
            "POSTGRES_USER=agentibridge\n"
            "POSTGRES_PASSWORD=agentibridge\n"
            "POSTGRES_DB=agentibridge\n"
        )
        with (
            patch("agentibridge.cli._STACK_DIR", stack_dir),
            patch("agentibridge.cli._LEGACY_STACK_DIR", tmp_path / "legacy"),
        ):
            result = _ensure_stack_dir()
        output = capsys.readouterr().out
        assert "Migrated" in output
        assert (stack_dir / "agentibridge.env").exists()
        assert result == stack_dir


@pytest.mark.unit
class TestReadEnvValue:
    """Tests for _read_env_value()."""

    def test_reads_existing_key(self, tmp_path):
        env = tmp_path / "test.env"
        env.write_text("FOO=bar\nBAZ=qux\n")
        assert _read_env_value("FOO", env) == "bar"
        assert _read_env_value("BAZ", env) == "qux"

    def test_returns_none_for_missing_key(self, tmp_path):
        env = tmp_path / "test.env"
        env.write_text("FOO=bar\n")
        assert _read_env_value("MISSING", env) is None

    def test_skips_comments(self, tmp_path):
        env = tmp_path / "test.env"
        env.write_text("# FOO=commented\nFOO=real\n")
        assert _read_env_value("FOO", env) == "real"

    def test_skips_lines_without_equals(self, tmp_path):
        env = tmp_path / "test.env"
        env.write_text("no_equals_here\nKEY=value\n")
        assert _read_env_value("KEY", env) == "value"


@pytest.mark.unit
class TestCmdInstall:
    """Tests for cmd_install() command."""

    def test_darwin_installs_both_launchd_agents(self, tmp_path):
        """Darwin install writes both the main and DB plists and loads both."""
        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli._STACK_DIR", tmp_path / "stack"),
            patch("agentibridge.cli._LEGACY_STACK_DIR", tmp_path / "legacy"),
            patch("agentibridge.cli._LAUNCHD_AGENTS_DIR", tmp_path / "LaunchAgents"),
            patch("agentibridge.cli._LAUNCHD_LOG_DIR", tmp_path / "Logs"),
            patch("agentibridge.cli.install_claude_assets"),
            patch("agentibridge.cli.subprocess.run", return_value=_ok()) as mock_run,
        ):
            cmd_install(MagicMock())

        assert (tmp_path / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist").exists()
        assert (tmp_path / "LaunchAgents" / f"{_LAUNCHD_DB_LABEL}.plist").exists()

        bootstrap_calls = [c[0][0] for c in mock_run.call_args_list if c[0][0][:2] == ["launchctl", "bootstrap"]]
        assert any(f"{_LAUNCHD_LABEL}.plist" in call[-1] for call in bootstrap_calls)
        assert any(f"{_LAUNCHD_DB_LABEL}.plist" in call[-1] for call in bootstrap_calls)

        compose_calls = [c[0][0] for c in mock_run.call_args_list]
        assert ["docker", "compose", "up", "-d"] in compose_calls
        # systemd path must not run on Darwin
        assert not any(call[0] == "systemctl" for call in compose_calls)

        # Reinstall pre-cleanup must boot out BOTH labels — a stale
        # com.agentibridge.db job would otherwise keep its old
        # ProgramArguments (bootstrap at the same path is a no-op for an
        # already-loaded job).
        bootout_calls = [c[0][0] for c in mock_run.call_args_list if c[0][0][:2] == ["launchctl", "bootout"]]
        assert any(call[-1].endswith(f"/{_LAUNCHD_LABEL}") for call in bootout_calls)
        assert any(call[-1].endswith(f"/{_LAUNCHD_DB_LABEL}") for call in bootout_calls)

    def test_linux_installs_systemd_units_not_launchd(self, tmp_path):
        """Linux install stays on the systemd path and never touches launchd."""
        with (
            patch("agentibridge.cli.platform.system", return_value="Linux"),
            patch("agentibridge.cli._STACK_DIR", tmp_path / "stack"),
            patch("agentibridge.cli._LEGACY_STACK_DIR", tmp_path / "legacy"),
            patch("agentibridge.cli.install_claude_assets"),
            patch("agentibridge.cli.subprocess.run", return_value=_ok()) as mock_run,
        ):
            cmd_install(MagicMock())

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert not any(call[0] == "launchctl" for call in calls)
        assert ["systemctl", "--user", "daemon-reload"] in calls
        assert (tmp_path / "LaunchAgents").exists() is False


@pytest.mark.unit
class TestCmdUninstall:
    """Tests for cmd_uninstall() command."""

    def test_darwin_removes_both_plists(self, tmp_path):
        agents_dir = tmp_path / "LaunchAgents"
        agents_dir.mkdir()
        main_plist = agents_dir / f"{_LAUNCHD_LABEL}.plist"
        db_plist = agents_dir / f"{_LAUNCHD_DB_LABEL}.plist"
        main_plist.write_bytes(b"")
        db_plist.write_bytes(b"")

        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli._LAUNCHD_AGENTS_DIR", agents_dir),
            patch("agentibridge.cli.uninstall_claude_assets"),
            patch("agentibridge.cli.subprocess.run", return_value=_ok()) as mock_run,
        ):
            cmd_uninstall(MagicMock())

        assert not main_plist.exists()
        assert not db_plist.exists()
        bootout_calls = [c[0][0] for c in mock_run.call_args_list if c[0][0][:2] == ["launchctl", "bootout"]]
        assert any(_LAUNCHD_LABEL in call[2] for call in bootout_calls)
        assert any(_LAUNCHD_DB_LABEL in call[2] for call in bootout_calls)

    def test_darwin_handles_missing_plists(self, tmp_path):
        """No plist on disk — bootout still runs, unlink is skipped without error."""
        agents_dir = tmp_path / "LaunchAgents"
        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli._LAUNCHD_AGENTS_DIR", agents_dir),
            patch("agentibridge.cli.uninstall_claude_assets"),
            patch("agentibridge.cli.subprocess.run", return_value=_ok()) as mock_run,
        ):
            cmd_uninstall(MagicMock())

        bootout_calls = [c[0][0] for c in mock_run.call_args_list if c[0][0][:2] == ["launchctl", "bootout"]]
        assert len(bootout_calls) == 2


@pytest.mark.unit
class TestCmdStop:
    """Tests for cmd_stop() command."""

    def test_stops_systemd_services(self):
        """Calls systemctl stop for both services."""
        with (
            patch("agentibridge.cli.platform.system", return_value="Linux"),
            patch("agentibridge.cli.subprocess.run") as mock_run,
        ):
            cmd_stop(MagicMock())

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert ["systemctl", "--user", "stop", "agentibridge"] in calls
        assert ["systemctl", "--user", "stop", "agentibridge-db"] in calls

    def test_stops_launchd_agents_on_darwin(self, tmp_path):
        """Darwin branch boots out both the main and DB launchd labels."""
        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli._STACK_DIR", tmp_path),
            patch("agentibridge.cli.subprocess.run") as mock_run,
        ):
            cmd_stop(MagicMock())

        bootout_calls = [c[0][0] for c in mock_run.call_args_list if c[0][0][:2] == ["launchctl", "bootout"]]
        assert any(_LAUNCHD_LABEL in call[2] for call in bootout_calls)
        assert any(_LAUNCHD_DB_LABEL in call[2] for call in bootout_calls)
        compose_calls = [c[0][0] for c in mock_run.call_args_list]
        assert ["docker", "compose", "down"] in compose_calls


@pytest.mark.unit
class TestCmdRestart:
    """Tests for cmd_restart() command."""

    def test_restarts_systemd_services(self):
        with (
            patch("agentibridge.cli.platform.system", return_value="Linux"),
            patch("agentibridge.cli.subprocess.run", return_value=_ok()) as mock_run,
        ):
            cmd_restart(MagicMock())

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert ["systemctl", "--user", "restart", "agentibridge-db"] in calls
        assert ["systemctl", "--user", "restart", "agentibridge"] in calls

    def test_darwin_boots_out_and_reloads_both_agents(self, tmp_path):
        """Darwin branch boots out both labels, then reloads whichever plists exist."""
        agents_dir = tmp_path / "LaunchAgents"
        agents_dir.mkdir()
        (agents_dir / f"{_LAUNCHD_LABEL}.plist").write_bytes(b"")
        (agents_dir / f"{_LAUNCHD_DB_LABEL}.plist").write_bytes(b"")

        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli._STACK_DIR", tmp_path),
            patch("agentibridge.cli._LAUNCHD_AGENTS_DIR", agents_dir),
            patch("agentibridge.cli.subprocess.run", return_value=_ok()) as mock_run,
        ):
            cmd_restart(MagicMock())

        calls = [c[0][0] for c in mock_run.call_args_list]
        bootout_calls = [c for c in calls if c[:2] == ["launchctl", "bootout"]]
        assert any(_LAUNCHD_LABEL in c[2] for c in bootout_calls)
        assert any(_LAUNCHD_DB_LABEL in c[2] for c in bootout_calls)

        bootstrap_calls = [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
        assert any(f"{_LAUNCHD_LABEL}.plist" in c[-1] for c in bootstrap_calls)
        assert any(f"{_LAUNCHD_DB_LABEL}.plist" in c[-1] for c in bootstrap_calls)

        assert ["docker", "compose", "restart"] in calls

    def test_darwin_skips_reload_for_missing_plist(self, tmp_path):
        """No plists on disk — bootstrap is never attempted."""
        agents_dir = tmp_path / "LaunchAgents"
        agents_dir.mkdir()

        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli._STACK_DIR", tmp_path),
            patch("agentibridge.cli._LAUNCHD_AGENTS_DIR", agents_dir),
            patch("agentibridge.cli.subprocess.run", return_value=_ok()) as mock_run,
        ):
            cmd_restart(MagicMock())

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert not any(c[:2] == ["launchctl", "bootstrap"] for c in calls)


@pytest.mark.unit
class TestCmdLogs:
    """Tests for cmd_logs() command."""

    def test_darwin_tails_launchd_log_file(self, tmp_path):
        args = MagicMock(tail=50, follow=False)
        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli._LAUNCHD_LOG_DIR", tmp_path),
            patch("agentibridge.cli.subprocess.run") as mock_run,
        ):
            cmd_logs(args)

        mock_run.assert_called_once_with(
            ["tail", "-n", "50", str(tmp_path / "agentibridge.log")],
            check=False,
        )

    def test_darwin_follow_appends_flag(self, tmp_path):
        args = MagicMock(tail=100, follow=True)
        with (
            patch("agentibridge.cli.platform.system", return_value="Darwin"),
            patch("agentibridge.cli._LAUNCHD_LOG_DIR", tmp_path),
            patch("agentibridge.cli.subprocess.run") as mock_run,
        ):
            cmd_logs(args)

        mock_run.assert_called_once_with(
            ["tail", "-n", "100", "-f", str(tmp_path / "agentibridge.log")],
            check=False,
        )

    def test_linux_uses_journalctl(self):
        args = MagicMock(tail=25, follow=True)
        with (
            patch("agentibridge.cli.platform.system", return_value="Linux"),
            patch("agentibridge.cli.subprocess.run") as mock_run,
        ):
            cmd_logs(args)

        mock_run.assert_called_once_with(
            ["journalctl", "--user", "-u", "agentibridge", "--no-pager", "-n", "25", "-f"],
            check=False,
        )


@pytest.mark.unit
class TestCmdEmbeddings:
    """Tests for agentibridge embeddings command."""

    def _make_args(self, check_llm=False):
        args = MagicMock()
        args.check_llm = check_llm
        return args

    def _mock_redis(self, return_value=None):
        mock_mod = MagicMock(get_redis=MagicMock(return_value=return_value))
        return patch.dict("sys.modules", {"agentibridge.redis_client": mock_mod})

    def _mock_pg(self, return_value=None):
        mock_mod = MagicMock(get_pg=MagicMock(return_value=return_value))
        return patch.dict("sys.modules", {"agentibridge.pg_client": mock_mod})

    def _mock_llm(self, embed_text=None, side_effect=None):
        mock_mod = MagicMock()
        if side_effect:
            mock_mod.embed_text.side_effect = side_effect
        elif embed_text is not None:
            mock_mod.embed_text.return_value = embed_text
        return patch.dict("sys.modules", {"agentibridge.llm_client": mock_mod})

    def _native_mode(self):
        """Patch stack dir to non-existent path (no env file loaded)."""
        from pathlib import Path

        return patch("agentibridge.cli._STACK_DIR", Path("/tmp/nonexistent-agentibridge"))

    # ── Native mode tests ──────────────────────────────────────────────

    def test_no_env_vars(self, capsys):
        """Shows 'not configured' when no env vars are set."""
        env = {
            "LLM_API_BASE": "",
            "LLM_API_KEY": "",
            "POSTGRES_URL": "",
            "DATABASE_URL": "",
            "AGENTIBRIDGE_EMBEDDING_ENABLED": "false",
        }
        with (
            self._native_mode(),
            patch.dict("os.environ", env, clear=False),
            self._mock_redis(),
            self._mock_pg(),
        ):
            cmd_embeddings(self._make_args())
        output = capsys.readouterr().out
        assert "Embedding Status" in output
        assert "source: agentibridge.env" in output
        assert "AGENTIBRIDGE_EMBEDDING_ENABLED: false" in output
        assert "LLM_API_BASE: (not set)" in output
        assert "LLM_API_KEY: (not set)" in output
        assert "not configured (LLM_API_BASE and LLM_API_KEY required)" in output
        assert "not configured (POSTGRES_URL not set)" in output

    def test_config_values_shown(self, capsys):
        """Shows config values when env vars are set."""
        env = {
            "LLM_API_BASE": "https://llm.example.com/v1",
            "LLM_API_KEY": "sk-test-key-abcdef123456",
            "LLM_EMBED_MODEL": "text-embedding-3-small",
            "PGVECTOR_DIMENSIONS": "1536",
            "AGENTIBRIDGE_EMBEDDING_ENABLED": "true",
            "POSTGRES_URL": "",
            "DATABASE_URL": "",
        }
        with (
            self._native_mode(),
            patch.dict("os.environ", env, clear=False),
            self._mock_redis(),
            self._mock_pg(),
        ):
            cmd_embeddings(self._make_args())
        output = capsys.readouterr().out
        assert "AGENTIBRIDGE_EMBEDDING_ENABLED: true" in output
        assert "LLM_API_BASE: https://llm.example.com/v1" in output
        assert "sk-tes...3456" in output
        assert "text-embedding-3-small" in output
        assert "configured (use --check-llm to test connectivity)" in output

    def test_api_key_redacted_short(self, capsys):
        """Short API keys are fully masked."""
        env = {
            "LLM_API_BASE": "https://llm.example.com/v1",
            "LLM_API_KEY": "shortkey",
            "POSTGRES_URL": "",
            "DATABASE_URL": "",
        }
        with (
            self._native_mode(),
            patch.dict("os.environ", env, clear=False),
            self._mock_redis(),
            self._mock_pg(),
        ):
            cmd_embeddings(self._make_args())
        output = capsys.readouterr().out
        assert "LLM_API_KEY: ***" in output

    def test_check_llm_reachable(self, capsys):
        """--check-llm calls embed_text and shows vector dim in native mode."""
        env = {
            "LLM_API_BASE": "https://llm.example.com/v1",
            "LLM_API_KEY": "sk-test-key-abcdef123456",
            "POSTGRES_URL": "",
            "DATABASE_URL": "",
        }
        with (
            self._native_mode(),
            patch.dict("os.environ", env, clear=False),
            self._mock_llm(embed_text=[0.1] * 1536),
            self._mock_redis(),
            self._mock_pg(),
        ):
            cmd_embeddings(self._make_args(check_llm=True))
        output = capsys.readouterr().out
        assert "reachable (returned 1536-dim vector)" in output

    def test_check_llm_error(self, capsys):
        """--check-llm shows error when endpoint fails."""
        env = {
            "LLM_API_BASE": "https://llm.example.com/v1",
            "LLM_API_KEY": "sk-test-key-abcdef123456",
            "POSTGRES_URL": "",
            "DATABASE_URL": "",
        }
        with (
            self._native_mode(),
            patch.dict("os.environ", env, clear=False),
            self._mock_llm(side_effect=RuntimeError("connection refused")),
            self._mock_redis(),
            self._mock_pg(),
        ):
            cmd_embeddings(self._make_args(check_llm=True))
        output = capsys.readouterr().out
        assert "status: error (connection refused)" in output

    def test_postgres_stats(self, capsys):
        """Shows chunk/session counts from Postgres in native mode."""
        env = {
            "LLM_API_BASE": "",
            "LLM_API_KEY": "",
            "POSTGRES_URL": "postgresql://localhost:5432/test",
        }
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.side_effect = [
            (True,),  # table exists
            (150, 10),  # count, distinct sessions
        ]
        mock_pool = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        with (
            self._native_mode(),
            patch.dict("os.environ", env, clear=False),
            self._mock_pg(mock_pool),
            self._mock_redis(),
        ):
            cmd_embeddings(self._make_args())
        output = capsys.readouterr().out
        assert "status: connected" in output
        assert "total chunks: 150" in output
        assert "sessions embedded: 10" in output

    def test_postgres_no_table(self, capsys):
        """Shows 'table not created yet' when transcript_chunks doesn't exist."""
        env = {
            "LLM_API_BASE": "",
            "LLM_API_KEY": "",
            "POSTGRES_URL": "postgresql://localhost:5432/test",
        }
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (False,)
        mock_pool = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        with (
            self._native_mode(),
            patch.dict("os.environ", env, clear=False),
            self._mock_pg(mock_pool),
            self._mock_redis(),
        ):
            cmd_embeddings(self._make_args())
        output = capsys.readouterr().out
        assert "connected (table not created yet)" in output

    def test_coverage_with_redis(self, capsys):
        """Shows coverage percentage when Redis is available in native mode."""
        env = {
            "LLM_API_BASE": "",
            "LLM_API_KEY": "",
            "POSTGRES_URL": "",
            "DATABASE_URL": "",
            "REDIS_KEY_PREFIX": "agentibridge",
        }
        mock_redis = MagicMock()
        mock_redis.zcard.return_value = 100

        with (
            self._native_mode(),
            patch.dict("os.environ", env, clear=False),
            self._mock_redis(mock_redis),
            self._mock_pg(),
        ):
            cmd_embeddings(self._make_args())
        output = capsys.readouterr().out
        assert "total sessions in Redis: 100" in output
        assert "sessions with embeddings: 0" in output
        assert "coverage: 0.0%" in output

    # ── Env file tests ──────────────────────────────────────────────

    def test_reads_from_env_file(self, capsys, tmp_path):
        """Config values are read from agentibridge.env."""
        env_file = tmp_path / "agentibridge.env"
        env_file.write_text(
            "AGENTIBRIDGE_EMBEDDING_ENABLED=true\n"
            "LLM_API_BASE=https://llm.test/v1\n"
            "LLM_API_KEY=sk-test-key-1234\n"
            "LLM_EMBED_MODEL=nomic-embed-text\n"
            "PGVECTOR_DIMENSIONS=768\n"
            "REDIS_KEY_PREFIX=agentibridge\n"
        )

        mock_get_pg = MagicMock(return_value=None)
        mock_get_redis = MagicMock(return_value=None)

        with (
            patch("agentibridge.cli._STACK_DIR", tmp_path),
            patch("agentibridge.cli._container_health", return_value=None),
            patch.dict(
                "sys.modules",
                {
                    "agentibridge.pg_client": MagicMock(get_pg=mock_get_pg),
                    "agentibridge.redis_client": MagicMock(get_redis=mock_get_redis),
                },
            ),
        ):
            cmd_embeddings(self._make_args())
        output = capsys.readouterr().out
        assert "source: agentibridge.env" in output
        assert "AGENTIBRIDGE_EMBEDDING_ENABLED: true" in output
        assert "nomic-embed-text" in output
        assert "768" in output
