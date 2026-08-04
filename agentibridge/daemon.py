"""Daemon lifecycle for the agentibridge network transport.

Under stdio the client spawns one server per session and reaps it — there is
no lifecycle problem. Under a network transport (sse) "something must already
be listening" is our problem: if nothing is running, the client has no tools,
silently. This module owns that lifecycle.

Two backends, selected by whether a user systemd bus exists:

- ``systemd`` — ``systemctl --user`` drives the ``agentibridge`` unit; the
  unit supervises (Restart=always) and survives reboots.
- ``pidfile`` — a detached child running ``agentibridge serve --sse`` with
  its pid recorded in ``~/.agentibridge/mcp-daemon.pid``. No supervisor: it
  dies with the machine, so it is one ``agentibridge daemon start`` per boot.

``AGENTIBRIDGE_MCP_SUPERVISOR=systemd|pidfile`` forces either.

The decision-to-spawn and the spawn itself run inside one flock — outside
it, two concurrent starts both decide to spawn and the loser overwrites the
pidfile with a pid that is about to die on the taken port. Liveness requires
the ``serve``/``--sse`` argv pair in ``/proc/<pid>/cmdline``, not a bare
``kill -0`` — a recycled pid would pass that and ``stop()`` would then signal
an unrelated process. ``stop()`` always tears down BOTH backends: a machine
that had systemd and lost it can hold state in both, and stopping one leaves
the other serving a port nothing points at.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".agentibridge"
LOCK_FILE = STATE_DIR / "mcp-daemon.lock"
PID_FILE = STATE_DIR / "mcp-daemon.pid"
LOG_DIR = STATE_DIR / "logs"
LOG_FILE = LOG_DIR / "mcp-daemon.log"

UNIT = "agentibridge"
DB_UNIT = "agentibridge-db"

# Adjacent argv pair identifying our pidfile-backend child.
_CMDLINE_MARKER = ("serve", "--sse")

_START_POLL_SECONDS = 15.0
_STOP_POLL_SECONDS = 4.0


def _read_env_file_value(key: str) -> str:
    """Read one ``KEY=value`` line from the state-dir env file (last wins)."""
    env_file = STATE_DIR / "agentibridge.env"
    value = ""
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
    return value


def daemon_port() -> int:
    raw = _read_env_file_value("AGENTIBRIDGE_PORT") or os.getenv("AGENTIBRIDGE_PORT", "8100")
    try:
        return int(raw)
    except ValueError:
        return 8100


def supervisor() -> str:
    """Backend selection: forced via env, else probe for a user systemd bus.

    ``show-environment`` is the probe on purpose: ``is-active`` also exits
    non-zero for a merely stopped unit, so it cannot distinguish "not
    running" from "no user bus at all".
    """
    forced = os.getenv("AGENTIBRIDGE_MCP_SUPERVISOR", "")
    if forced in ("systemd", "pidfile"):
        return forced
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return "pidfile"
    return "systemd" if result.returncode == 0 else "pidfile"


def _read_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a for a in raw.decode(errors="replace").split("\0") if a]


def pid_alive(pid: int) -> bool:
    """kill -0 plus the argv-marker check — a recycled pid must not pass."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    argv = _read_cmdline(pid)
    return any((argv[i], argv[i + 1]) == _CMDLINE_MARKER for i in range(len(argv) - 1))


def _read_pidfile() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=0.5):
            return True
    except OSError:
        return False


def _systemd_unit_active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", UNIT],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return (result.stdout or "").strip() == "active"


def _systemd_unit_failed() -> bool:
    """True only for a unit in the terminal ``failed`` state — distinguishes
    "crashed" from "still activating", which ``is-active`` cannot."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-failed", UNIT],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return (result.stdout or "").strip() == "failed"


def _stop_systemd() -> None:
    try:
        subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True, check=False)
    except OSError:
        pass


def _stop_pidfile() -> None:
    pid = _read_pidfile()
    if pid is None:
        return
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + _STOP_POLL_SECONDS
        while time.monotonic() < deadline and pid_alive(pid):
            time.sleep(0.1)
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    PID_FILE.unlink(missing_ok=True)


def stop() -> None:
    """Tear down BOTH backends, regardless of which one is detected."""
    _stop_systemd()
    _stop_pidfile()


def _spawn_pidfile_child(env_file: Path) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    bridge_bin = shutil.which("agentibridge")
    cmd = [bridge_bin, "serve", "--sse"] if bridge_bin else [sys.executable, "-m", "agentibridge.cli", "serve", "--sse"]
    # Transport is carried by the --sse flag, and port/host come from the
    # env file the child loads — none may be inherited. A stray
    # AGENTIBRIDGE_PORT/HOST/TRANSPORT export in the invoking shell would
    # win over the env file (load_dotenv override=False) and the child
    # would silently serve a port nothing is registered against.
    _scrub = ("AGENTIBRIDGE_TRANSPORT", "AGENTIBRIDGE_PORT", "AGENTIBRIDGE_HOST")
    child_env = {k: v for k, v in os.environ.items() if k not in _scrub}
    child_env["AGENTIBRIDGE_ENV_FILE"] = str(env_file)
    log_handle = LOG_FILE.open("ab")
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            env=child_env,
        )
    finally:
        log_handle.close()


def _start_systemd() -> None:
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
    for unit in (DB_UNIT, UNIT):
        subprocess.run(["systemctl", "--user", "enable", "--now", unit], capture_output=True, check=False)
    subprocess.run(["systemctl", "--user", "restart", UNIT], capture_output=True, check=False)


def _start_databases(stack_dir: Path) -> None:
    compose = stack_dir / "docker-compose.yml"
    if not compose.is_file():
        return
    try:
        subprocess.run(["docker", "compose", "up", "-d"], cwd=stack_dir, capture_output=True, check=False)
    except OSError:
        print("  [--] docker not available — databases not started (filesystem fallback active)")


def ensure_running(
    *,
    restart: bool = False,
    env_file: Path | None = None,
    stack_dir: Path | None = None,
    backend: str | None = None,
) -> bool:
    """Converge the daemon process onto the current config.

    ``restart=True`` is unconditional-restart: stop whatever is running,
    then start on current config. No change-detection — the installer has
    just rewritten the url/unit/port, and comparing every input that could
    have drifted is more failure surface than a sub-second restart costs.
    """
    env_file = env_file or (STATE_DIR / "agentibridge.env")
    stack_dir = stack_dir or STATE_DIR
    port = daemon_port()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if restart:
                stop()
            else:
                pid = _read_pidfile()
                if (pid is not None and pid_alive(pid)) or _systemd_unit_active():
                    return True

            chosen = backend or supervisor()
            child_pid: int | None = None
            if chosen == "systemd":
                _start_systemd()
            else:
                _start_databases(stack_dir)
                proc = _spawn_pidfile_child(env_file)
                child_pid = proc.pid
                PID_FILE.write_text(f"{child_pid}\n")

            # Liveness before port: an open port is not proof OUR child is
            # serving it — a daemon we failed to stop answers identically.
            deadline = time.monotonic() + _START_POLL_SECONDS
            while time.monotonic() < deadline:
                if child_pid is not None and not pid_alive(child_pid):
                    print(f"  [!!] daemon exited during startup — see {LOG_FILE}")
                    return False
                if chosen == "systemd" and _systemd_unit_failed():
                    print(f"  [!!] {UNIT} unit entered failed state — see journalctl --user -u {UNIT}")
                    return False
                if port_open(port):
                    print(f"  Daemon serving on localhost:{port} ({chosen} backend)")
                    return True
                time.sleep(0.2)
            where = LOG_FILE if chosen == "pidfile" else f"journalctl --user -u {UNIT}"
            print(f"  [!!] daemon did not open port {port} within {_START_POLL_SECONDS:.0f}s — see {where}")
            return False
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def status() -> dict:
    pid = _read_pidfile()
    port = daemon_port()
    return {
        "backend": supervisor(),
        "systemd_unit_active": _systemd_unit_active(),
        "pidfile_pid": pid,
        "pidfile_alive": pid_alive(pid) if pid is not None else False,
        "port": port,
        "port_open": port_open(port),
    }
