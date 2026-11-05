"""Master agents: interactive Claude Code sessions on this workstation that claude.ai can chat with.

``agentihooks init-agent`` opens a herdr tab in ``AGENTIBRIDGE_MASTER_DIR`` and runs
``agentihooks claude --route claude-ai`` there with Remote Control on.
"""

import os
import subprocess
import time
from pathlib import Path

from agentibridge.config import AGENTIBRIDGE_MASTER_DIR, AGENTIHOOKS_BINARY

LAUNCH_TIMEOUT_S = 300
STRIPPED_ENV = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def default_name() -> str:
    """A session name unique to the second: ``master-<yymmdd-hhmmss>``."""
    return f"master-{time.strftime('%y%m%d-%H%M%S')}"


def launch_command(name: str, prompt: str) -> list[str]:
    """The ``agentihooks init-agent`` argv that opens master agent ``name``."""
    return [
        AGENTIHOOKS_BINARY,
        "init-agent",
        "--agent",
        "claude",
        "--dir",
        AGENTIBRIDGE_MASTER_DIR,
        "--name",
        name,
        *(["--prompt", prompt] if prompt else []),
        "--",
        "--route",
        "claude-ai",
        "--remote-control",
        name,
    ]


def launch_environment() -> dict[str, str]:
    """The bridge environment without Anthropic auth overrides, the agentihooks directory first on PATH."""
    env = {key: value for key, value in os.environ.items() if key not in STRIPPED_ENV}
    env["PATH"] = os.pathsep.join(filter(None, [str(Path(AGENTIHOOKS_BINARY).parent), env.get("PATH", "")]))
    return env


def parse_report(output: str) -> dict[str, str]:
    """The ``key=value`` lines of an init-agent report."""
    pairs = (line.partition("=") for line in output.splitlines())
    return {key.strip(): value.strip() for key, found, value in pairs if found and key.strip()}


def launch_master(name: str = "", prompt: str = "") -> dict:
    """Open a master agent; success only once init-agent reports the session routed."""
    name = name or default_name()
    result = {"success": False, "name": name, "directory": AGENTIBRIDGE_MASTER_DIR}
    try:
        done = subprocess.run(
            launch_command(name, prompt),
            env=launch_environment(),
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=LAUNCH_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**result, "error": str(exc)}
    report = parse_report(done.stdout)
    if done.returncode == 0 and report.get("route_status") == "routed":
        return {**result, "success": True, "report": report}
    reason = done.stderr.strip() or report.get("route_error") or f"route_status={report.get('route_status', 'none')}"
    return {**result, "report": report, "error": reason}
