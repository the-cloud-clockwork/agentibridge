"""Run one bash or PowerShell command on the bridge host, with a preflight of the host's OS and shells."""

import contextlib
import io
import os
import platform
import shutil
import signal
import subprocess
import threading
import time
from functools import cache
from pathlib import Path
from typing import Any

OUTPUT_LIMIT = 65536
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 600
DRAIN_GRACE_S = 2.0
WSL_POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
ENV_KEYS = (
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TZ",
    "PATH",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
    "WSLENV",
    "SYSTEMROOT",
    "COMSPEC",
    "USERPROFILE",
    "TEMP",
    "TMP",
)


def _distribution(system: str) -> str:
    """A readable OS name: the Linux distribution, the macOS version or the Windows release."""
    if system == "Linux":
        with contextlib.suppress(OSError):
            return platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
        return "Linux"
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    return f"{system} {platform.release()}"


def _powershell() -> str:
    """The PowerShell binary: pwsh, then Windows PowerShell on PATH or through WSL interop."""
    found = shutil.which("pwsh") or shutil.which("powershell") or shutil.which("powershell.exe")
    return found or (str(WSL_POWERSHELL) if WSL_POWERSHELL.exists() else "")


@cache
def preflight() -> dict[str, Any]:
    """The host facts a caller needs before composing a command."""
    system = platform.system()
    shells = {
        name: path for name, path in (("bash", shutil.which("bash") or ""), ("powershell", _powershell())) if path
    }
    return {
        "os": system,
        "distribution": _distribution(system),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "wsl": system == "Linux" and "microsoft" in platform.release().lower(),
        "hostname": platform.node(),
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "shells": shells,
        "default_shell": "powershell" if system == "Windows" else "bash",
    }


def describe(host: dict[str, Any]) -> str:
    """The run_command tool description, carrying the host preflight."""
    wsl = " (WSL)" if host["wsl"] else ""
    shells = ", ".join(host["shells"]) or "none"
    return (
        "Run one bash or PowerShell command on this workstation and return its exit code, stdout and stderr.\n\n"
        f"Host: {host['distribution']}{wsl}, {host['os']} {host['kernel']}, {host['arch']}, "
        f"user {host['user']} on {host['hostname']}.\n"
        f"Shells: {shells}. Default: {host['default_shell']}.\n\n"
        "stdin is closed, so interactive prompts fail instead of hanging. Each stream is capped at 64 KiB "
        "(truncated=true when cut). At `timeout` seconds (max 600) the whole process group is killed and the "
        "partial output returns with timed_out=true. When the command ends, anything it left running in its "
        "process group is killed; a process that starts its own session keeps running. The environment is "
        "minimal: no login profile is loaded.\n\n"
        f"Commands run as {host['user']} and can read every file that user can, credentials under the home "
        "directory included.\n\n"
        "Args:\n"
        "    command: The command line to run\n"
        "    shell: bash or powershell (empty: the host default)\n"
        "    timeout: Seconds before the command is killed (default 60)\n"
        "    cwd: Working directory (default: home)"
    )


def command_environment() -> dict[str, str]:
    """The allowlisted environment a command runs with, ~/.local/bin first on PATH."""
    env = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
    env["PATH"] = os.pathsep.join(filter(None, [str(Path.home() / ".local" / "bin"), env.get("PATH", "")]))
    env["TERM"] = "dumb"
    return env


def _drain(stream: io.BufferedIOBase, kept: bytearray, seen: list[int]) -> None:
    """Read ``stream`` to its end, keeping the first OUTPUT_LIMIT bytes and counting all of them."""
    for chunk in iter(lambda: stream.read1(OUTPUT_LIMIT), b""):
        seen[0] += len(chunk)
        kept += chunk[: max(0, OUTPUT_LIMIT - len(kept))]


def _kill_group(process: subprocess.Popen) -> None:
    """Kill the command and everything it started in its process group."""
    with contextlib.suppress(ProcessLookupError):
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()


def _argv(shell: str, path: str, command: str) -> list[str]:
    """The argv that runs ``command`` in ``shell`` without loading a profile."""
    if shell == "bash":
        return [path, "-c", command]
    return [path, "-NoProfile", "-NonInteractive", "-Command", command]


def run_command(command: str, shell: str = "", timeout: int = DEFAULT_TIMEOUT_S, cwd: str = "") -> dict[str, Any]:
    """Run ``command`` and return its exit code and output; its process group dies when it exits or times out."""
    host = preflight()
    chosen = shell.strip().lower() or host["default_shell"]
    path = host["shells"].get(chosen)
    if not path:
        available = ", ".join(host["shells"]) or "none"
        return {"success": False, "error": f"shell {chosen!r} is not available on this host; available: {available}"}
    workdir = Path(cwd).expanduser() if cwd else Path.home()
    if not workdir.is_dir():
        return {"success": False, "error": f"no such directory: {workdir}"}
    started = time.monotonic()
    process = subprocess.Popen(
        _argv(chosen, path, command),
        cwd=workdir,
        env=command_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    outputs = {name: (bytearray(), [0]) for name in ("stdout", "stderr")}
    readers = [
        threading.Thread(target=_drain, args=(stream, *outputs[name]), daemon=True)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=max(1, min(timeout, MAX_TIMEOUT_S)))
    except subprocess.TimeoutExpired:
        timed_out = True
    _kill_group(process)
    process.wait()
    for reader in readers:
        reader.join(DRAIN_GRACE_S)
    return {
        "success": process.returncode == 0 and not timed_out,
        "shell": chosen,
        "cwd": str(workdir),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout": bytes(outputs["stdout"][0]).decode("utf-8", errors="replace"),
        "stderr": bytes(outputs["stderr"][0]).decode("utf-8", errors="replace"),
        "truncated": any(seen[0] > OUTPUT_LIMIT for _, seen in outputs.values()),
    }
