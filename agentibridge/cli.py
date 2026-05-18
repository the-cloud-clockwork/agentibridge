"""CLI helper tool for agentibridge.

Provides commands for status, config, connection strings, and service management.

Usage:
    agentibridge install    — Install as systemd service (databases + native agentibridge)
    agentibridge update     — Update to the latest version (pip + Docker)
    agentibridge stop       — Stop agentibridge + databases
    agentibridge restart    — Restart the stack
    agentibridge logs       — View agentibridge logs
    agentibridge status     — Check service status and connectivity
    agentibridge help       — Available MCP tools and configuration reference
    agentibridge connect    — Connection strings for Claude Code, ChatGPT, etc.
    agentibridge config     — Current config dump / generate .env template
    agentibridge tunnel     — Cloudflare Tunnel status and URL
    agentibridge locks      — Show Redis keys and file locks
    agentibridge install    — Install as systemd user service
    agentibridge uninstall  — Remove systemd service
    agentibridge version    — Print version
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agentibridge.claude_assets import install_claude_assets, uninstall_claude_assets


DATA_DIR = Path(__file__).parent / "data"
_ENV_FILE = "agentibridge.env"
_DOCKER_PS_FORMAT = "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
_CLOUDFLARED_DIR = ".cloudflared"
_CLOUDFLARED_CONFIG = "config.yml"
_NOT_SET = "(not set)"
_MCP_JSON_HINT = "Add to ~/.mcp.json:"


def _version() -> str:
    from agentibridge import __version__

    return __version__


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize ~/.agentibridge/ state directory and verify prerequisites."""
    import json
    from datetime import datetime, timezone

    state_dir = Path.home() / ".agentibridge"
    logs_dir = state_dir / "logs"
    state_file = state_dir / "state.json"

    state_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Read or create state.json
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            pass

    state["source"] = str(Path(__file__).resolve().parent.parent)
    state["version"] = _version()
    state["installed_at"] = datetime.now(timezone.utc).isoformat()
    state_file.write_text(json.dumps(state, indent=2))

    print(f"State directory: {state_dir}")
    print(f"Version: {state['version']}")

    # Check Docker
    docker_ok = shutil.which("docker") is not None
    print(f"Docker: {'available' if docker_ok else 'NOT FOUND'}")

    if not docker_ok:
        print("Warning: Docker is required for agentibridge run/stop/restart")

    print("Done.")


def cmd_version(args: argparse.Namespace) -> None:
    print(f"agentibridge {_version()}")


def _container_health(name: str) -> str | None:
    """Return container health/status string, or None if not found."""
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                name,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _systemd_active(service: str) -> str | None:
    """Return systemd unit active state, or None if unavailable."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _cloudflared_hostname() -> str | None:
    """Try to extract tunnel hostname from ~/.cloudflared/config.yml."""
    return _parse_cloudflared_config().get("hostname")


def cmd_status(args: argparse.Namespace) -> None:
    print(f"AgentiBridge v{_version()}")
    print("=" * 50)

    env_file = _STACK_DIR / _ENV_FILE

    # --- Service ---
    print("\n[Service]")
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "agentibridge"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = result.stdout.strip()
        print(f"  systemd: {status}")
    except Exception:
        print("  systemd: not checked (systemctl unavailable)")

    # Check Docker
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", "agentibridge"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            print(f"  docker:  {result.stdout.strip()}")
        else:
            print("  docker:  not running")
    except Exception:
        print("  docker:  not checked (docker unavailable)")

    # --- Docker Stack ---
    print("\n[Docker Stack]")
    for container in ["agentibridge", "agentibridge-redis", "agentibridge-postgres"]:
        health = _container_health(container)
        if health is not None:
            print(f"  {container}: {health}")
        else:
            print(f"  {container}: not found")

    # --- Redis ---
    print("\n[Redis]")
    health = _container_health("agentibridge-redis") or "not running"
    print(f"  container: {health}")
    try:
        from agentibridge.redis_client import get_redis

        r = get_redis()
        if r is not None:
            r.ping()
            print("  status: connected")
            from agentibridge.store import _rkey

            count = r.zcard(_rkey("idx:all"))
            print(f"  sessions indexed: {count}")
        else:
            url = os.getenv("REDIS_URL") or (_read_env_value("REDIS_URL", env_file) if env_file.exists() else _NOT_SET)
            print(f"  status: unavailable (REDIS_URL={url})")
    except Exception as e:
        print(f"  status: error ({e})")

    # --- Postgres ---
    print("\n[Postgres]")
    health = _container_health("agentibridge-postgres") or "not running"
    print(f"  container: {health}")
    try:
        from agentibridge.pg_client import get_pg

        pool = get_pg()
        if pool is not None:
            with pool.connection() as conn:
                row = conn.execute("SELECT COUNT(*), COUNT(DISTINCT session_id) FROM transcript_chunks").fetchone()
                print("  status: connected")
                print(f"  chunks indexed: {row[0]}")
                print(f"  sessions with embeddings: {row[1]}")
        else:
            url = os.getenv("POSTGRES_URL") or (
                _read_env_value("POSTGRES_URL", env_file) if env_file.exists() else _NOT_SET
            )
            print(f"  status: unavailable (POSTGRES_URL={url})")
    except Exception as e:
        print(f"  status: error ({e})")

    # --- Tunnel ---
    print("\n[Tunnel]")
    tunnel_shown = False

    # Check Docker container
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", "agentibridge-tunnel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            tunnel_status = result.stdout.strip()
            print(f"  cloudflared: {tunnel_status} (docker)")
            tunnel_shown = True
            if tunnel_status == "running":
                log_result = subprocess.run(
                    ["docker", "logs", "--tail", "50", "agentibridge-tunnel"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                log_output = log_result.stdout + log_result.stderr
                url = _extract_tunnel_url(log_output)
                if url:
                    print(f"  url: {url}")
                elif "Starting named tunnel" in log_output:
                    print("  mode: named tunnel")
                else:
                    print("  url: (detecting...)")
    except Exception:
        pass

    # Check systemd cloudflared
    systemd_state = _systemd_active("cloudflared")
    if systemd_state == "active":
        print(f"  cloudflared: {systemd_state} (systemd)")
        tunnel_shown = True
        hostname = _cloudflared_hostname()
        if hostname:
            print(f"  hostname: {hostname}")

    if not tunnel_shown:
        print("  cloudflared: not running")

    # --- Transcripts ---
    print("\n[Transcripts]")
    claude_home = Path(os.getenv("CLAUDE_CODE_HOME_DIR", str(Path.home() / ".claude")))
    projects_dir = claude_home / "projects"
    if projects_dir.exists():
        jsonl_count = sum(1 for _ in projects_dir.rglob("*.jsonl"))
        print(f"  directory: {projects_dir}")
        print(f"  JSONL files: {jsonl_count}")
    else:
        print(f"  directory: {projects_dir} (not found)")

    # --- Config ---
    print("\n[Config]")
    if env_file.exists():
        transport = _read_env_value("AGENTIBRIDGE_TRANSPORT", env_file) or "sse"
        port = _read_env_value("AGENTIBRIDGE_PORT", env_file) or "8100"
        poll = _read_env_value("AGENTIBRIDGE_POLL_INTERVAL", env_file) or "60"
    else:
        transport = os.getenv("AGENTIBRIDGE_TRANSPORT", "sse")
        port = os.getenv("AGENTIBRIDGE_PORT", "8100")
        poll = os.getenv("AGENTIBRIDGE_POLL_INTERVAL", "60")
    print(f"  transport: {transport}")
    print(f"  port: {port}")
    print(f"  poll interval: {poll}s")
    print(f"  env file: {env_file}")


def cmd_help(args: argparse.Namespace) -> None:
    print(f"AgentiBridge v{_version()} — Claude CLI Transcript MCP Server")
    print("=" * 60)
    print()
    print("MCP TOOLS (17 total)")
    print("-" * 60)
    print()
    print("Phase 1 — Foundation:")
    print("  list_sessions        List sessions across all projects")
    print("  get_session          Get full session metadata + transcript")
    print("  get_session_segment  Paginated/time-range transcript retrieval")
    print("  get_session_actions  Extract tool calls with counts")
    print("  search_sessions      Keyword search across transcripts")
    print("  collect_now          Trigger immediate collection")
    print()
    print("Phase 2 — Semantic Search:")
    print("  search_semantic      Semantic search using embeddings")
    print("  generate_summary     AI-generated session summary")
    print()
    print("Phase 4 — Write-back & Dispatch:")
    print("  restore_session      Load session context for continuation")
    print("  dispatch_task        Dispatch task with session context")
    print("  get_dispatch_job     Poll a dispatch job for status/output")
    print("  list_dispatch_jobs   List dispatch jobs with status filter")
    print()
    print("Phase 5 — Knowledge Catalog:")
    print("  list_memory_files    List memory files across projects")
    print("  get_memory_file      Read a specific memory file")
    print("  list_plans           List plans sorted by recency")
    print("  get_plan             Read a plan by codename")
    print("  search_history       Search the global prompt history")
    print()
    print("CONFIGURATION")
    print("-" * 60)
    print()
    print("  REDIS_URL                       Redis connection URL")
    print("  AGENTIBRIDGE_EMBEDDING_ENABLED  Enable semantic search (default: false)")
    print("  AGENTIBRIDGE_TRANSPORT          stdio or sse (default: stdio)")
    print("  AGENTIBRIDGE_HOST               Bind address (default: 127.0.0.1)")
    print("  AGENTIBRIDGE_PORT               HTTP port (default: 8100)")
    print("  AGENTIBRIDGE_API_KEYS           Comma-separated API keys")
    print("  AGENTIBRIDGE_POLL_INTERVAL      Poll interval in seconds (default: 60)")
    print("  AGENTIBRIDGE_MAX_ENTRIES        Max entries per session (default: 500)")
    print("  CLAUDE_CODE_HOME_DIR            Claude Code home directory (~/.claude)")
    print("  POSTGRES_URL                    Postgres connection URL (pgvector)")
    print("  PGVECTOR_DIMENSIONS             Embedding vector dimensions (default: 1536)")
    print("  LLM_API_BASE                    OpenAI-compatible API base URL")
    print("  LLM_API_KEY                     API key for LLM endpoint")
    print("  LLM_EMBED_MODEL                Embedding model name")
    print("  LLM_CHAT_MODEL                 Chat model for summaries (fallback)")
    print("  ANTHROPIC_API_KEY               Anthropic key for summaries (preferred)")
    print("  ANTHROPIC_AUTH_TOKEN            Auth token for LLM proxies (alternative)")
    print("  ANTHROPIC_BASE_URL              Base URL for LLM proxies")
    print("  CLAUDE_BINARY                   Path to Claude CLI (default: claude)")
    print("  CLAUDE_DISPATCH_MODEL           Dispatch model (default: sonnet)")
    print("  CLAUDE_DISPATCH_TIMEOUT         Dispatch timeout in seconds (default: 300)")
    print("  CLOUDFLARE_TUNNEL_TOKEN         Token for named Cloudflare Tunnel (optional)")
    print()
    print("USAGE")
    print("-" * 60)
    print()
    print("  Local (stdio):   python -m agentibridge")
    print("  Remote (SSE):    AGENTIBRIDGE_TRANSPORT=sse python -m agentibridge")
    print("  Docker:          docker compose up --build -d")
    print("  Tunnel:          docker compose --profile tunnel up -d")
    print()
    print("Run 'agentibridge connect' for client connection strings.")


def cmd_connect(args: argparse.Namespace) -> None:
    host = args.host or os.getenv("AGENTIBRIDGE_HOST", "localhost")
    port = args.port or os.getenv("AGENTIBRIDGE_PORT", "8100")
    api_key = args.api_key or "your-api-key"

    print(f"Connection strings for {host}:{port}")
    print("=" * 60)

    print()
    print("=== Claude Code CLI ===")
    print(_MCP_JSON_HINT)
    config = {
        "mcpServers": {
            "agentibridge": {
                "type": "http",
                "url": f"http://{host}:{port}/sse",
                "headers": {"X-API-Key": api_key},
            }
        }
    }
    print(json.dumps(config, indent=2))

    print()
    print("=== ChatGPT Custom GPT / Actions ===")
    print(f"  Actions URL: http://{host}:{port}/sse")
    print("  Auth: API Key in X-API-Key header")
    print(f"  Key: {api_key}")

    print()
    print("=== Claude Web (MCP) ===")
    print(f"  URL: http://{host}:{port}/sse")
    print(f"  Header: X-API-Key: {api_key}")

    print()
    print("=== Generic API ===")
    print(f"  SSE endpoint:  http://{host}:{port}/sse")
    print(f"  Health check:  http://{host}:{port}/health")
    print(f"  Auth header:   X-API-Key: {api_key}")

    print()
    print("=== Cloudflare Tunnel ===")
    print("  Start a quick tunnel (no account needed):")
    print("    docker compose --profile tunnel up -d")
    print("  Then run 'agentibridge tunnel' to get the public URL.")

    print()
    print("=== curl test ===")
    print(f"  curl -s http://{host}:{port}/health")


def _extract_tunnel_url(log_output: str) -> str | None:
    """Extract *.trycloudflare.com URL from cloudflared log output."""
    match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", log_output)
    return match.group(0) if match else None


def _parse_cloudflared_config() -> dict:
    """Parse ~/.cloudflared/config.yml for tunnel details."""
    cfg = Path.home() / _CLOUDFLARED_DIR / _CLOUDFLARED_CONFIG
    if not cfg.exists():
        return {}
    info: dict = {}
    try:
        for line in cfg.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("tunnel:"):
                info["tunnel_id"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("- hostname:"):
                info["hostname"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("hostname:"):
                info["hostname"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("service:") and "http" in stripped:
                info["service"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("credentials-file:"):
                info["credentials_file"] = stripped.split(":", 1)[1].strip()
    except Exception:
        pass
    return info


def _print_systemd_tunnel_status() -> bool:
    """Print systemd cloudflared status. Returns True if active."""
    systemd_state = _systemd_active("cloudflared")
    if systemd_state != "active":
        return False

    print("Cloudflare Tunnel: active (systemd)")
    print()

    cfg = _parse_cloudflared_config()
    if cfg:
        print(f"[Config] (~/{_CLOUDFLARED_DIR}/{_CLOUDFLARED_CONFIG})")
        if "tunnel_id" in cfg:
            print(f"  tunnel id: {cfg['tunnel_id']}")
        if "hostname" in cfg:
            print(f"  hostname:  {cfg['hostname']}")
        if "service" in cfg:
            print(f"  service:   {cfg['service']}")
        if "credentials_file" in cfg:
            print(f"  creds:     {cfg['credentials_file']}")
        print()

    hostname = cfg.get("hostname")
    if hostname:
        print("Test:")
        print(f"  curl -s https://{hostname}/health")
        print()
        print(_MCP_JSON_HINT)
        mcp_config = {
            "mcpServers": {
                "agentibridge": {
                    "type": "http",
                    "url": f"https://{hostname}/mcp",
                }
            }
        }
        api_keys = os.getenv("AGENTIBRIDGE_API_KEYS", "")
        if api_keys:
            first_key = api_keys.split(",")[0].strip()
            mcp_config["mcpServers"]["agentibridge"]["headers"] = {"X-API-Key": first_key}
        print(json.dumps(mcp_config, indent=2))

    return True


def _check_docker_tunnel() -> bool:
    """Check Docker tunnel container status. Returns True if found."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", "agentibridge-tunnel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False

    status = result.stdout.strip()
    print(f"Cloudflare Tunnel: {status} (docker)")

    if status != "running":
        print("Container exists but is not running. Check logs:")
        print("  docker logs agentibridge-tunnel")
        return True

    # Read logs to detect mode and URL
    try:
        log_result = subprocess.run(
            ["docker", "logs", "--tail", "50", "agentibridge-tunnel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        print("Could not read tunnel container logs.")
        return True

    log_output = log_result.stdout + log_result.stderr

    url = _extract_tunnel_url(log_output)
    if url:
        _print_quick_tunnel(url)
        return True

    if "Starting named tunnel" in log_output:
        print("Mode: named tunnel")
        print("The tunnel is connected via your Cloudflare configuration.")
        print("Check your Cloudflare Zero Trust dashboard for the hostname.")
        print()
        print("Logs:")
        print("  docker logs agentibridge-tunnel")

    return True


def _print_quick_tunnel(url: str) -> None:
    """Print quick tunnel URL and MCP config snippet."""
    print("Mode: quick tunnel")
    print(f"URL:  {url}")
    print()
    print(_MCP_JSON_HINT)
    config: dict = {
        "mcpServers": {
            "agentibridge": {
                "type": "http",
                "url": f"{url}/sse",
            }
        }
    }
    api_keys = os.getenv("AGENTIBRIDGE_API_KEYS", "")
    if api_keys:
        first_key = api_keys.split(",")[0].strip()
        config["mcpServers"]["agentibridge"]["headers"] = {"X-API-Key": first_key}
    print(json.dumps(config, indent=2))
    print()
    print("Test:")
    print(f"  curl -s {url}/health")


def _cmd_tunnel_status() -> None:
    """Show Cloudflare Tunnel status — checks Docker container and systemd."""
    if _check_docker_tunnel():
        return

    if _print_systemd_tunnel_status():
        return

    print("Cloudflare Tunnel is not running.")
    print()
    print("Start a quick tunnel (no Cloudflare account needed):")
    print("  docker compose --profile tunnel up -d")
    print()
    print("Start a named tunnel (persistent hostname):")
    print("  CLOUDFLARE_TUNNEL_TOKEN=xxx docker compose --profile tunnel up -d")
    print()
    print("Set up a named tunnel interactively:")
    print("  agentibridge tunnel setup")

    # Unknown state
    print("Tunnel is running but could not determine mode.")
    print("Check logs: docker logs agentibridge-tunnel")


def _cmd_tunnel_setup() -> None:
    """Interactive 10-step wizard to install and configure a named cloudflared tunnel."""
    # Step 1 — Install cloudflared
    if not shutil.which("cloudflared"):
        print("Step 1: Installing cloudflared...")
        system = platform.system()
        machine = platform.machine()
        if system == "Linux":
            arch_map = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}
            arch = arch_map.get(machine)
            if not arch:
                print(f"ERROR: Unsupported architecture {machine}")
                sys.exit(1)
            url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
            dest = "/usr/local/bin/cloudflared"
            subprocess.run(["sudo", "curl", "-fsSL", url, "-o", dest], check=True)
            subprocess.run(["sudo", "chmod", "+x", dest], check=True)
        elif system == "Darwin":
            subprocess.run(["brew", "install", "cloudflared"], check=True)
        else:
            print(f"ERROR: Unsupported OS {system}")
            sys.exit(1)
        print("  cloudflared installed.")
    else:
        print("Step 1: cloudflared already installed.")

    # Step 2 — Authenticate
    print("Step 2: Checking cloudflared authentication...")
    result = subprocess.run(["cloudflared", "tunnel", "list"], capture_output=True)
    if result.returncode != 0:
        print("  Launching browser login...")
        subprocess.run(["cloudflared", "tunnel", "login"], check=True)

    # Step 3 — Prompt tunnel name
    name = input("Step 3: Tunnel name [agentibridge]: ").strip() or "agentibridge"

    # Step 4 — Create tunnel (idempotent)
    print(f"Step 4: Looking up or creating tunnel '{name}'...")
    raw = subprocess.run(
        ["cloudflared", "tunnel", "list", "-o", "json"],
        capture_output=True,
        text=True,
    ).stdout
    tunnels = json.loads(raw or "[]")
    tunnel_id = next(
        (t["id"] for t in tunnels if t["name"] == name and not t.get("deleted_at")),
        None,
    )
    if not tunnel_id:
        out = subprocess.run(
            ["cloudflared", "tunnel", "create", name],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        # Re-query list for ID
        raw = subprocess.run(
            ["cloudflared", "tunnel", "list", "-o", "json"],
            capture_output=True,
            text=True,
        ).stdout
        tunnels = json.loads(raw or "[]")
        tunnel_id = next((t["id"] for t in tunnels if t["name"] == name), None)
        if not tunnel_id:
            m = re.search(r"with id ([0-9a-f-]+)", out)
            tunnel_id = m.group(1) if m else None
        if not tunnel_id:
            print("ERROR: Could not determine tunnel ID")
            sys.exit(1)
    print(f"  Tunnel ID: {tunnel_id}")

    # Steps 5+6 — Prompt subdomain + domain
    subdomain = input("Step 5: Subdomain (e.g. mcp): ").strip()
    domain = input("Step 6: Domain (e.g. example.com): ").strip()
    hostname = f"{subdomain}.{domain}"

    # Step 7 — DNS route
    print(f"Step 7: Setting DNS route for {hostname}...")
    subprocess.run(
        ["cloudflared", "tunnel", "route", "dns", name, hostname],
        check=False,  # idempotent — may already exist
    )

    # Step 8 — Write config.yml
    print(f"Step 8: Writing ~/{_CLOUDFLARED_DIR}/{_CLOUDFLARED_CONFIG}...")
    config_dir = Path.home() / _CLOUDFLARED_DIR
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / _CLOUDFLARED_CONFIG
    creds_file = config_dir / f"{tunnel_id}.json"
    port = os.getenv("AGENTIBRIDGE_PORT", "8100")
    desired = (
        f"tunnel: {tunnel_id}\n"
        f"credentials-file: {creds_file}\n\n"
        f"ingress:\n"
        f"  - hostname: {hostname}\n"
        f"    service: http://localhost:{port}\n"
        f"  - service: http_status:404\n"
    )
    if config_file.exists() and config_file.read_text() != desired:
        backup = config_file.with_suffix(f".yml.bak.{int(time.time())}")
        shutil.copy2(config_file, backup)
        print(f"  Backed up existing config to {backup}")
    config_file.write_text(desired)
    print(f"  Written: {config_file}")

    # Step 9 — Optional systemd service (Linux only)
    if platform.system() == "Linux":
        print("Step 9: Systemd service setup...")
        already = (
            subprocess.run(
                ["systemctl", "is-enabled", "cloudflared"],
                capture_output=True,
            ).returncode
            == 0
        )
        cf_bin = shutil.which("cloudflared")
        if already:
            subprocess.run(["sudo", "systemctl", "restart", "cloudflared"])
            print("  Restarted existing cloudflared service.")
        else:
            answer = input("  Install cloudflared as systemd service? [y/N]: ").strip().lower()
            if answer == "y":
                subprocess.run(
                    ["sudo", cf_bin, "--config", str(config_file), "service", "install"],
                    check=True,
                )
                subprocess.run(
                    ["sudo", "systemctl", "enable", "--now", "cloudflared"],
                    check=True,
                )
                print("  cloudflared service enabled and started.")
            else:
                print(f"  Run manually: cloudflared tunnel run {name}")
    else:
        print(f"Step 9: Run manually: cloudflared tunnel run {name}")

    # Step 10 — Health check
    print("Step 10: Verifying tunnel health check...")
    time.sleep(2)
    result = subprocess.run(
        ["curl", "-sf", "--max-time", "10", f"https://{hostname}/health"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"  Health check passed: {result.stdout.strip()}")
    else:
        print(f"  Health check pending — verify with: curl https://{hostname}/health")

    print()
    print(f"Setup complete! Your tunnel: https://{hostname}")


def cmd_tunnel(args: argparse.Namespace) -> None:
    action = getattr(args, "action", "status")
    if action == "setup":
        _cmd_tunnel_setup()
    else:
        _cmd_tunnel_status()


def cmd_config(args: argparse.Namespace) -> None:
    if args.generate_env:
        _generate_env_template()
        return

    print("Current Configuration")
    print("=" * 50)

    env_vars = [
        ("REDIS_URL", ""),
        ("REDIS_KEY_PREFIX", "agentibridge"),
        ("AGENTIBRIDGE_TRANSPORT", "stdio"),
        ("AGENTIBRIDGE_HOST", "127.0.0.1"),
        ("AGENTIBRIDGE_PORT", "8100"),
        ("AGENTIBRIDGE_API_KEYS", ""),
        ("AGENTIBRIDGE_POLL_INTERVAL", "60"),
        ("AGENTIBRIDGE_MAX_ENTRIES", "500"),
        ("CLAUDE_CODE_HOME_DIR", str(Path.home() / ".claude")),
        ("AGENTIBRIDGE_ENABLED", "true"),
        ("POSTGRES_URL", ""),
        ("PGVECTOR_DIMENSIONS", "1536"),
        ("LLM_API_BASE", ""),
        ("LLM_API_KEY", ""),
        ("LLM_EMBED_MODEL", ""),
        ("LLM_CHAT_MODEL", ""),
        ("ANTHROPIC_API_KEY", ""),
        ("ANTHROPIC_AUTH_TOKEN", ""),
        ("ANTHROPIC_BASE_URL", ""),
        ("CLAUDE_BINARY", "claude"),
        ("CLAUDE_DISPATCH_MODEL", "sonnet"),
        ("CLAUDE_DISPATCH_TIMEOUT", "300"),
        ("CLAUDE_HOOK_LOG_ENABLED", "true"),
        ("AGENTIBRIDGE_LOG_FILE", ""),
    ]

    for key, default in env_vars:
        val = os.getenv(key, "")
        source = "env" if val else "default"
        fallback = default if default else _NOT_SET
        display = val if val else fallback
        print(f"  {key}={display}  [{source}]")


def _generate_env_template() -> None:
    template = """# AgentiBridge Configuration
# Copy to ~/.agentibridge/.env or .env

# Redis (optional — falls back to filesystem)
# REDIS_URL=redis://localhost:6379/0
# REDIS_KEY_PREFIX=agentibridge

# Transport: stdio (local MCP) or sse (HTTP remote)
AGENTIBRIDGE_TRANSPORT=stdio
AGENTIBRIDGE_HOST=127.0.0.1
AGENTIBRIDGE_PORT=8100

# API key auth for SSE transport (comma-separated, empty = no auth)
# AGENTIBRIDGE_API_KEYS=key1,key2

# Collector
AGENTIBRIDGE_POLL_INTERVAL=60
AGENTIBRIDGE_MAX_ENTRIES=500
# CLAUDE_CODE_HOME_DIR=~/.claude

# Postgres + pgvector (required for semantic search vector storage)
# POSTGRES_URL=postgresql://DB_USER:DB_PASSWORD@localhost:5432/agentibridge
# PGVECTOR_DIMENSIONS=1536

# Semantic search + LLM (OpenAI-compatible API)
# LLM_API_BASE=http://localhost:11434/v1
# LLM_API_KEY=
# LLM_EMBED_MODEL=text-embedding-3-small
# LLM_CHAT_MODEL=gpt-4o-mini

# Summary generation (Anthropic SDK preferred, falls back to LLM_CHAT_MODEL)
# ANTHROPIC_API_KEY=

# Dispatch (Claude CLI)
# CLAUDE_BINARY=claude
# CLAUDE_DISPATCH_MODEL=sonnet
# CLAUDE_DISPATCH_TIMEOUT=300

# Logging
CLAUDE_HOOK_LOG_ENABLED=true
# AGENTIBRIDGE_LOG_FILE=~/.cache/agentibridge/agentibridge.log

# Cloudflare Tunnel (optional — use docker compose --profile tunnel)
# CLOUDFLARE_TUNNEL_TOKEN=your-tunnel-token-here
"""
    print(template)


def cmd_serve(args: argparse.Namespace) -> None:
    """Run the MCP server in stdio (default) or sse mode.

    Loads ``$AGENTIBRIDGE_ENV_FILE`` (or ``~/.agentibridge/agentibridge.env``) before
    delegating to :func:`agentibridge.server.main`. Stdio mode is what
    Claude Code spawns via the user-scope ``.mcp.json`` registration.
    """
    env_file = Path(os.getenv("AGENTIBRIDGE_ENV_FILE") or (Path.home() / ".agentibridge" / "agentibridge.env"))
    if env_file.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)
        except ImportError:
            for line in env_file.read_text().splitlines():
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    if args.stdio:
        os.environ["AGENTIBRIDGE_TRANSPORT"] = "stdio"
    elif args.sse:
        os.environ["AGENTIBRIDGE_TRANSPORT"] = "sse"

    from agentibridge.server import main as server_main

    server_main()


def cmd_install(args: argparse.Namespace) -> None:
    systemd_dir = Path.home() / ".config" / "systemd" / "user"

    print("Installing agentibridge as systemd user service")

    # Stop existing services
    for unit in ("agentibridge", "agentibridge-bridge", "agentibridge-db"):
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, check=False)
        subprocess.run(["systemctl", "--user", "disable", unit], capture_output=True, check=False)
    # Stop old agentibridge container if running from previous install
    subprocess.run(["docker", "stop", "agentibridge"], capture_output=True, check=False)
    subprocess.run(["docker", "rm", "agentibridge"], capture_output=True, check=False)
    # Kill stale processes
    subprocess.run(["pkill", "-f", "python.*-m agentibridge"], capture_output=True, check=False)

    # Ensure stack dir and env
    stack_dir = _ensure_stack_dir()
    env_file = stack_dir / _ENV_FILE

    # Always refresh compose file (removes old agentibridge container service)
    compose_dest = stack_dir / "docker-compose.yml"
    shutil.copy2(DATA_DIR / "docker-compose.yml", compose_dest)
    print(f"  Updated {compose_dest}")

    # Migrate old Docker-internal hostnames to localhost
    if env_file.exists():
        env_text = env_file.read_text()
        new_text = env_text.replace("://redis:", "://localhost:").replace("@postgres:", "@localhost:")
        # Remove obsolete bridge vars
        for var in ("CLAUDE_DISPATCH_URL", "DISPATCH_SECRET", "DISPATCH_BRIDGE_PORT"):
            new_text = re.sub(rf"^#?\s*{var}=.*\n?", "", new_text, flags=re.MULTILINE)
        if new_text != env_text:
            env_file.write_text(new_text)
            print("  Migrated agentibridge.env: localhost + removed bridge vars")

    # Ensure CLAUDE_BINARY is set to absolute path
    if env_file.exists():
        claude_bin = shutil.which("claude") or "claude"
        env_text = env_file.read_text()
        if "CLAUDE_BINARY=" not in env_text:
            with open(env_file, "a") as f:
                f.write(f"\nCLAUDE_BINARY={claude_bin}\n")
            print(f"  Added CLAUDE_BINARY={claude_bin}")

    # Generate systemd service with current Python path
    python_bin = sys.executable
    systemd_dir.mkdir(parents=True, exist_ok=True)

    # Database service (docker compose for Redis + Postgres)
    db_dest = systemd_dir / "agentibridge-db.service"
    db_dest.write_text(
        "[Unit]\n"
        "Description=AgentiBridge Databases (Redis + Postgres)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"WorkingDirectory={stack_dir}\n"
        f"ExecStart=/usr/bin/docker compose up -d\n"
        f"ExecStop=/usr/bin/docker compose down\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    print(f"  Installed {db_dest}")

    # AgentiBridge native service
    svc_dest = systemd_dir / "agentibridge.service"
    svc_dest.write_text(
        "[Unit]\n"
        "Description=AgentiBridge MCP Server (native)\n"
        "After=network.target agentibridge-db.service\n"
        "Wants=agentibridge-db.service\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"EnvironmentFile=-{env_file}\n"
        "Environment=AGENTIBRIDGE_TRANSPORT=sse\n"
        f"ExecStart={python_bin} -m agentibridge\n"
        f"WorkingDirectory={Path.home()}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    print(f"  Installed {svc_dest} (python: {python_bin})")

    # Remove old bridge service if present
    bridge_svc = systemd_dir / "agentibridge-bridge.service"
    if bridge_svc.exists():
        bridge_svc.unlink()
        print(f"  Removed obsolete {bridge_svc}")

    # Enable and start
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    for unit in ["agentibridge-db", "agentibridge"]:
        subprocess.run(["systemctl", "--user", "enable", unit], check=False)
        result = subprocess.run(["systemctl", "--user", "start", unit], check=False)
        if result.returncode == 0:
            print(f"  {unit}: enabled and started")
        else:
            print(f"  {unit}: enabled (start failed — check journalctl --user -u {unit})")
    print()

    try:
        install_claude_assets()
    except Exception as exc:
        print(f"  [!!] Claude asset install skipped: {exc}")

    print()
    print("Check status with: agentibridge status")
    print("View logs with: journalctl --user -u agentibridge -f")


def cmd_uninstall(args: argparse.Namespace) -> None:
    print("Uninstalling agentibridge systemd services...")

    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    for unit in ("agentibridge", "agentibridge-db", "agentibridge-bridge"):
        try:
            subprocess.run(["systemctl", "--user", "stop", unit], check=False)
            subprocess.run(["systemctl", "--user", "disable", unit], check=False)
        except Exception:
            pass
        svc = systemd_dir / f"{unit}.service"
        if svc.exists():
            svc.unlink()
            print(f"  Removed {svc}")

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    except Exception:
        pass

    print("  Services uninstalled")
    print()

    try:
        uninstall_claude_assets()
    except Exception as exc:
        print(f"  [!!] Claude asset uninstall skipped: {exc}")

    print()
    print("Note: Config files in ~/.agentibridge/ were preserved.")
    print("Remove manually if no longer needed.")


def cmd_locks(args: argparse.Namespace) -> None:
    """Show Redis keys, file position locks, and bridge resource state."""
    print(f"AgentiBridge v{_version()} — Lock & Resource Inspector")
    print("=" * 60)

    # ── Redis locks / keys ────────────────────────────────────────────
    print("\n[Redis Keys]")
    try:
        from agentibridge.redis_client import get_redis

        r = get_redis()
        if r is None:
            print("  Redis: unavailable (REDIS_URL not set or connection failed)")
        else:
            r.ping()
            prefix = os.getenv("REDIS_KEY_PREFIX", "agentibridge")

            # Session index
            idx_all = f"{prefix}:sb:idx:all"
            session_count = r.zcard(idx_all)
            print(f"  Session index ({idx_all}): {session_count} sessions")

            # Project indexes
            cursor = 0
            project_indexes = []
            while True:
                cursor, keys = r.scan(cursor, match=f"{prefix}:sb:idx:project:*", count=100)
                project_indexes.extend(keys)
                if cursor == 0:
                    break
            print(f"  Project indexes: {len(project_indexes)}")
            for key in sorted(project_indexes):
                count = r.zcard(key)
                # Extract project name from key
                proj_name = key.replace(f"{prefix}:sb:idx:project:", "")
                print(f"    {proj_name}: {count} sessions")

            # Position keys (collector file offsets)
            cursor = 0
            pos_keys = []
            while True:
                cursor, keys = r.scan(cursor, match=f"{prefix}:sb:pos:*", count=100)
                pos_keys.extend(keys)
                if cursor == 0:
                    break
            print(f"  Position locks (file offsets): {len(pos_keys)}")
            for key in sorted(pos_keys):
                val = r.get(key)
                short_key = key.replace(f"{prefix}:sb:pos:", "")
                print(f"    {short_key}: offset {val}")

            # Session data keys (meta + entries)
            cursor = 0
            meta_keys = []
            entry_keys = []
            while True:
                cursor, keys = r.scan(cursor, match=f"{prefix}:sb:session:*", count=100)
                for k in keys:
                    if k.endswith(":meta"):
                        meta_keys.append(k)
                    elif k.endswith(":entries"):
                        entry_keys.append(k)
                if cursor == 0:
                    break
            print(f"  Session metadata keys: {len(meta_keys)}")
            print(f"  Session entry lists: {len(entry_keys)}")

            # Total memory usage estimate
            info = r.info("memory")
            used_mb = info.get("used_memory_human", "unknown")
            print(f"  Redis memory usage: {used_mb}")

    except Exception as e:
        print(f"  Redis error: {e}")

    # ── File-based position locks ─────────────────────────────────────
    print("\n[File Position Locks]")
    pos_dir = Path(
        os.getenv(
            "AGENTIBRIDGE_POSITIONS_DIR",
            str(Path.home() / ".cache" / "agentibridge" / "positions"),
        )
    )
    if pos_dir.exists():
        pos_files = list(pos_dir.glob("*.pos"))
        print(f"  Directory: {pos_dir}")
        print(f"  Position files: {len(pos_files)}")
        for pf in sorted(pos_files):
            try:
                offset = pf.read_text().strip()
                print(f"    {pf.name}: offset {offset}")
            except OSError:
                print(f"    {pf.name}: (unreadable)")
    else:
        print(f"  Directory: {pos_dir} (not found — no file locks)")

    # ── Bridge process locks ──────────────────────────────────────────
    print("\n[Bridge Processes]")

    # Check for running agentibridge processes
    try:
        result = subprocess.run(
            ["pgrep", "-af", "agentibridge"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                print(f"  PID {line}")
        else:
            print("  No agentibridge processes found")
    except Exception:
        print("  Process check unavailable (pgrep not found)")

    # Docker containers
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=agentibridge", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            print("\n  Docker containers:")
            for line in result.stdout.strip().splitlines():
                print(f"    {line}")
        else:
            print("  No agentibridge Docker containers running")
    except Exception:
        print("  Docker check unavailable")

    if args.clear:
        print("\n[Clearing locks]")
        # Clear file position locks
        if pos_dir.exists():
            cleared = 0
            for pf in pos_dir.glob("*.pos"):
                pf.unlink()
                cleared += 1
            print(f"  Cleared {cleared} file position lock(s)")

        # Clear Redis position keys
        try:
            from agentibridge.redis_client import get_redis

            r = get_redis()
            if r is not None:
                prefix = os.getenv("REDIS_KEY_PREFIX", "agentibridge")
                cursor = 0
                cleared = 0
                while True:
                    cursor, keys = r.scan(cursor, match=f"{prefix}:sb:pos:*", count=100)
                    if keys:
                        r.delete(*keys)
                        cleared += len(keys)
                    if cursor == 0:
                        break
                print(f"  Cleared {cleared} Redis position key(s)")
        except Exception as e:
            print(f"  Redis clear failed: {e}")

        print("  Done. Next collection cycle will re-index from scratch.")


def _docker_exec_query(container: str, query: str, db_user: str = "agentibridge") -> str | None:
    """Run a psql query inside a Docker container, return stdout or None."""
    try:
        result = subprocess.run(
            ["docker", "exec", container, "psql", "-U", db_user, "-tAc", query],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _docker_exec_redis(container: str, *redis_args: str) -> str | None:
    """Run a redis-cli command inside a Docker container, return stdout or None."""
    try:
        result = subprocess.run(
            ["docker", "exec", container, "redis-cli", *redis_args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


_SEARCH_PROMPT_TMPL = (
    "You are a reconnaissance helper for the agentibridge fleet. "
    "The operator asked:\n\n"
    "  {query}\n\n"
    "Use the MCP tools available to you (list_sessions, search_sessions, "
    "search_history, search_semantic, get_session, list_memory_files, "
    "list_plans, plus Read/Glob/Grep) to find the most relevant sessions, "
    "files, history entries, memory files, or plans that match the query. "
    "Return ONLY a compact JSON object of the form:\n"
    '  {{"success": true, "query": "<echo>", "count": N, '
    '"matches": [ {{...relevant fields per hit...}} ], '
    '"notes": "<one short sentence of context, optional>"}}\n'
    "Put the most relevant hits first. No prose outside the JSON."
)


def _strip_json_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _render_search_human(result_text: str, query: str, duration_ms: int | None) -> str:
    raw = _strip_json_fence(result_text or "")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return result_text or "(no result)"

    lines = []
    dur = f"{(duration_ms or 0) / 1000:.1f}s" if duration_ms else "?"
    count = data.get("count", len(data.get("matches") or []))
    lines.append(f"\n🔎 {query}")
    lines.append(f"   {count} match(es)  ·  {dur}")
    notes = data.get("notes")
    if notes:
        lines.append(f"   note: {notes}")
    lines.append("")

    for i, m in enumerate(data.get("matches") or [], 1):
        if not isinstance(m, dict):
            lines.append(f"  {i}. {m}")
            continue
        # Headline: prefer type + id-ish field
        kind = m.get("type") or m.get("kind") or ""
        ident = (
            m.get("sha")
            or m.get("session_id")
            or m.get("pr")
            or m.get("file")
            or m.get("path")
            or m.get("codename")
            or ""
        )
        ts = m.get("timestamp") or m.get("updated_at") or m.get("date") or ""
        headline = " ".join(x for x in [kind, str(ident), ts] if x).strip()
        lines.append(f"  {i}. {headline}")

        for key in ("branch", "pr", "message", "file", "diff_stat", "summary", "details", "snippet"):
            val = m.get(key)
            if val:
                val_str = str(val).replace("\n", " ")
                if len(val_str) > 200:
                    val_str = val_str[:197] + "..."
                lines.append(f"       {key}: {val_str}")
        # Any other fields we didn't render explicitly
        rendered = {
            "type",
            "kind",
            "sha",
            "session_id",
            "pr",
            "file",
            "path",
            "codename",
            "timestamp",
            "updated_at",
            "date",
            "branch",
            "message",
            "diff_stat",
            "summary",
            "details",
            "snippet",
        }
        extras = {k: v for k, v in m.items() if k not in rendered}
        if extras:
            extra_str = ", ".join(f"{k}={v}" for k, v in extras.items())
            if len(extra_str) > 200:
                extra_str = extra_str[:197] + "..."
            lines.append(f"       {extra_str}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _fmt_tool_args(tool_name: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("query", "pattern", "path", "file_path", "session_id", "codename", "command"):
        if tool_input.get(key):
            val = str(tool_input[key])
            if len(val) > 80:
                val = val[:77] + "..."
            return val
    # fall back to the first string value
    for v in tool_input.values():
        if isinstance(v, str) and v:
            return v[:80]
    return ""


def cmd_search(args: argparse.Namespace) -> None:
    """Headless reconnaissance via streaming `claude -p`.

    Streams progress (tool calls) to stderr in real time; prints a
    human-readable summary on stdout when done (or raw JSON with --json).
    """
    query = " ".join(args.query).strip()
    if not query:
        print('usage: agentibridge search "<query>"', file=sys.stderr)
        sys.exit(2)

    prompt = _SEARCH_PROMPT_TMPL.format(query=query)
    if args.instructions:
        prompt += f"\n\nAdditional instructions:\n{args.instructions}"

    binary = os.environ.get("CLAUDE_BINARY", "claude")
    cmd = [
        binary,
        "--permission-mode",
        "bypassPermissions",
        "--model",
        args.model,
        "--output-format",
        "stream-json",
        "--verbose",
        "-p",
        prompt,
    ]

    clean_env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}

    stderr_is_tty = sys.stderr.isatty()
    dim = "\033[2m" if stderr_is_tty else ""
    reset = "\033[0m" if stderr_is_tty else ""
    bold = "\033[1m" if stderr_is_tty else ""

    print(f"{bold}🔎 {query}{reset}", file=sys.stderr)
    print(f"{dim}   spawning claude --model {args.model} (bypass, stream)...{reset}", file=sys.stderr)
    sys.stderr.flush()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_env,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print(f"claude CLI not found: {binary}", file=sys.stderr)
        sys.exit(127)

    final_result = None
    final_session_id = None
    final_duration_ms = None
    is_error = False
    started = time.time()

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")

            if etype == "system" and event.get("subtype") == "init":
                sid = event.get("session_id", "")
                print(f"{dim}   session {sid[:8]}{reset}", file=sys.stderr)

            elif etype == "assistant":
                for block in (event.get("message", {}) or {}).get("content", []) or []:
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name", "?")
                        arg_preview = _fmt_tool_args(name, block.get("input") or {})
                        elapsed = time.time() - started
                        print(
                            f"{dim}   [{elapsed:5.1f}s] → {name}({arg_preview}){reset}",
                            file=sys.stderr,
                        )
                    elif btype == "text":
                        txt = (block.get("text") or "").strip()
                        if txt and len(txt) < 120:
                            print(f"{dim}   … {txt}{reset}", file=sys.stderr)
                sys.stderr.flush()

            elif etype == "result":
                final_result = event.get("result") or ""
                final_session_id = event.get("session_id")
                final_duration_ms = event.get("duration_ms")
                is_error = bool(event.get("is_error"))

        proc.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        print("timed out", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        proc.kill()
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)

    if proc.returncode != 0 and not final_result:
        err = (proc.stderr.read() if proc.stderr else "") or f"exit {proc.returncode}"
        print(f"claude failed: {err[:500]}", file=sys.stderr)
        sys.exit(1)

    resume_cmd = f"claude --resume {final_session_id}" if final_session_id else ""

    if args.json:
        print(
            json.dumps(
                {
                    "success": not is_error,
                    "query": query,
                    "result": final_result,
                    "session_id": final_session_id,
                    "duration_ms": final_duration_ms,
                    "resume_command": resume_cmd,
                },
                indent=2,
            )
        )
    elif args.raw:
        print(final_result or "")
    else:
        print(_render_search_human(final_result or "", query, final_duration_ms))

    if final_session_id:
        footer_stream = sys.stderr if args.raw else sys.stdout
        print(f"{dim}session: {final_session_id}{reset}", file=footer_stream)
        print(f"{dim}resume:  {resume_cmd}{reset}", file=footer_stream)

    sys.exit(0 if not is_error else 1)


def cmd_embeddings(args: argparse.Namespace) -> None:
    """Show embedding pipeline status: config, LLM backend, Postgres vectors."""
    print(f"AgentiBridge v{_version()} — Embedding Status")
    print("=" * 60)

    env_file = _STACK_DIR / _ENV_FILE

    # ── Helper to read a value from agentibridge.env or shell env ────────────
    def _env(key: str, default: str = "") -> str:
        if env_file.exists():
            return _read_env_value(key, env_file) or default
        return os.getenv(key, default)

    # ── Config ─────────────────────────────────────────────────────────
    print("\n[Config]")
    embed_enabled = _env("AGENTIBRIDGE_EMBEDDING_ENABLED", "false")
    llm_base = _env("LLM_API_BASE")
    llm_key = _env("LLM_API_KEY")
    llm_model = _env("LLM_EMBED_MODEL", "text-embedding-3-small")
    pg_dims = _env("PGVECTOR_DIMENSIONS", "1536")

    print("  source: agentibridge.env")
    print(f"  AGENTIBRIDGE_EMBEDDING_ENABLED: {embed_enabled}")
    print(f"  LLM_API_BASE: {llm_base or _NOT_SET}")
    if llm_key:
        redacted = llm_key[:6] + "..." + llm_key[-4:] if len(llm_key) > 12 else "***"
        print(f"  LLM_API_KEY: {redacted}")
    else:
        print(f"  LLM_API_KEY: {_NOT_SET}")
    print(f"  LLM_EMBED_MODEL: {llm_model}")
    print(f"  PGVECTOR_DIMENSIONS: {pg_dims}")

    # ── LLM Backend ────────────────────────────────────────────────────
    print("\n[LLM Backend]")
    if not llm_base or not llm_key:
        print("  status: not configured (LLM_API_BASE and LLM_API_KEY required)")
    elif args.check_llm:
        try:
            from agentibridge.llm_client import embed_text

            vec = embed_text("test")
            print(f"  status: reachable (returned {len(vec)}-dim vector)")
        except Exception as e:
            print(f"  status: error ({e})")
    else:
        print("  status: configured (use --check-llm to test connectivity)")

    # ── Postgres ───────────────────────────────────────────────────────
    print("\n[Postgres]")
    sessions_with_embeddings = 0
    pg_url = _env("POSTGRES_URL") or os.getenv("POSTGRES_URL", "")
    if not pg_url:
        print("  status: not configured (POSTGRES_URL not set)")
    else:
        health = _container_health("agentibridge-postgres")
        if health:
            print(f"  container: {health}")
        try:
            from agentibridge.pg_client import get_pg

            pool = get_pg()
            if pool is None:
                print("  status: connection failed")
            else:
                with pool.connection() as conn:
                    row = conn.execute(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'transcript_chunks')"
                    ).fetchone()
                    if not row[0]:
                        print("  status: connected (table not created yet)")
                    else:
                        stats = conn.execute(
                            "SELECT COUNT(*), COUNT(DISTINCT session_id) FROM transcript_chunks"
                        ).fetchone()
                        sessions_with_embeddings = stats[1]
                        print("  status: connected")
                        print(f"  total chunks: {stats[0]:,}")
                        print(f"  sessions embedded: {sessions_with_embeddings:,}")
        except Exception as e:
            print(f"  status: error ({e})")

    # ── Coverage ───────────────────────────────────────────────────────
    print("\n[Coverage]")
    try:
        from agentibridge.redis_client import get_redis

        r = get_redis()
        if r is not None:
            prefix = _env("REDIS_KEY_PREFIX", "agentibridge")
            total_sessions = r.zcard(f"{prefix}:sb:idx:all")
            print(f"  total sessions in Redis: {total_sessions:,}")
            print(f"  sessions with embeddings: {sessions_with_embeddings:,}")
            if total_sessions > 0:
                pct = (sessions_with_embeddings / total_sessions) * 100
                print(f"  coverage: {pct:.1f}%")
            else:
                print("  coverage: N/A (no sessions indexed)")
        else:
            print("  Redis: unavailable")
    except Exception as e:
        print(f"  Redis error: {e}")


# ---------------------------------------------------------------------------
# Docker stack commands
# ---------------------------------------------------------------------------

_STACK_DIR = Path.home() / ".agentibridge"
_STATE_FILE = _STACK_DIR / "state.json"
_LEGACY_STACK_DIR = Path.home() / ".config" / "agentibridge"
_GITHUB_REPO_URL = "https://github.com/The-Cloud-Clockwork/agentibridge"

_REQUIRED_ENV_VARS = [
    "REDIS_URL",
    "POSTGRES_URL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "AGENTIBRIDGE_TRANSPORT",
    "AGENTIBRIDGE_PORT",
]


def _validate_env(env_file: Path) -> None:
    """Exit with error if any required variable is missing from agentibridge.env."""
    text = env_file.read_text()
    missing = [v for v in _REQUIRED_ENV_VARS if not re.search(rf"^\s*{v}=", text, re.MULTILINE)]
    if missing:
        print(f"ERROR: {env_file.name} is missing required variables:")
        for v in missing:
            print(f"  • {v}")
        print(f"\nReference: {env_file.parent / 'agentibridge.env.example'}")
        sys.exit(1)


def _ensure_stack_dir() -> Path:
    """Prepare ~/.agentibridge/ for docker compose operations.

    Migrates from legacy ~/.config/agentibridge/ if needed.
    Copies bundled compose file and agentibridge.env template on first run.
    Copies bundled agentibridge.env.example with working defaults on first run.
    """
    # Migrate legacy ~/.config/agentibridge/ → ~/.agentibridge/
    if _LEGACY_STACK_DIR.exists() and not _STACK_DIR.exists():
        shutil.move(str(_LEGACY_STACK_DIR), str(_STACK_DIR))
        print(f"Migrated {_LEGACY_STACK_DIR} → {_STACK_DIR}")

    _STACK_DIR.mkdir(parents=True, exist_ok=True)

    compose_dest = _STACK_DIR / "docker-compose.yml"
    if not compose_dest.exists():
        shutil.copy2(DATA_DIR / "docker-compose.yml", compose_dest)
        print(f"Created {compose_dest}")

    env_dest = _STACK_DIR / _ENV_FILE

    # Migration: rename docker.env or .env → agentibridge.env
    for old_name in ("docker.env", ".env"):
        old_env = _STACK_DIR / old_name
        if not env_dest.exists() and old_env.exists():
            shutil.move(str(old_env), str(env_dest))
            print(f"Migrated {old_env} → {env_dest}")
            break

    if not env_dest.exists():
        shutil.copy2(DATA_DIR / "agentibridge.env.example", env_dest)
        print(f"Created {env_dest}")
        print()
        print("Starting Docker with default configuration.")
        print(f"To customize, edit {env_dest}")
        print(f"Docs: {_GITHUB_REPO_URL}#readme")

    _validate_env(env_dest)
    return _STACK_DIR


def _detect_stack_state(stack_dir: Path) -> str:
    """Returns 'running', 'partial', or 'stopped'."""
    result = subprocess.run(
        _compose_cmd(stack_dir) + ["ps", "--format", "{{.State}}"],
        capture_output=True,
        text=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return "stopped"
    running = sum(1 for line in lines if "running" in line or line == "Up")
    if running == 0:
        return "stopped"
    return "running" if running == len(lines) else "partial"


def _compose_cmd(stack_dir: Path) -> list[str]:
    """Base docker compose invocation for the managed stack."""
    return [
        "docker",
        "compose",
        "-f",
        str(stack_dir / "docker-compose.yml"),
        "--env-file",
        str(stack_dir / _ENV_FILE),
    ]


def cmd_update(args: argparse.Namespace) -> None:
    """Update agentibridge to the latest version (pip package + Docker image)."""
    old_version = _version()
    print(f"AgentiBridge v{old_version}")
    print("=" * 50)

    # ── 1. Update pip package ─────────────────────────────────────────
    print("\n[pip] Upgrading agentibridge package...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "agentibridge"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[pip] ERROR: upgrade failed\n{result.stderr.strip()}")
        if not args.docker:
            sys.exit(1)
    else:
        # Reload version from the freshly installed package
        new_version = _get_installed_version()
        if new_version and new_version != old_version:
            print(f"[pip] Updated: {old_version} -> {new_version}")
        else:
            print(f"[pip] Already up to date ({old_version})")

    # ── 2. Update Docker stack (if --docker or stack is running) ──────
    has_docker = shutil.which("docker") is not None

    if args.docker or (has_docker and _is_stack_running()):
        if not has_docker:
            print("\n[docker] Skipped — docker is not installed")
        else:
            _update_docker_stack()
    elif has_docker:
        print("\n[docker] Stack is not running — skipped (use --docker to force)")

    print("\nUpdate complete.")


def _get_installed_version() -> str | None:
    """Query pip for the currently installed agentibridge version."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "show", "agentibridge"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def _is_stack_running() -> bool:
    """Check if the agentibridge Docker container exists and is running."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", "agentibridge"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "running"


def _update_docker_stack() -> None:
    """Pull latest image and recreate the agentibridge container."""
    stack_dir = _STACK_DIR
    compose_file = stack_dir / "docker-compose.yml"
    env_file = stack_dir / _ENV_FILE

    if not compose_file.exists() or not env_file.exists():
        print("\n[docker] Stack not initialised — run 'agentibridge run' first")
        return

    compose = _compose_cmd(stack_dir)

    # Capture current image digest
    old_digest = (
        subprocess.run(
            ["docker", "images", "--digests", "--no-trunc", "--format", "{{.Digest}}", "ghcr.io/the-cloud-clockwork/agentibridge"],
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .split("\n")[0]
    )

    # Pull latest image
    print("\n[docker] Pulling ghcr.io/the-cloud-clockwork/agentibridge:dev...")
    result = subprocess.run(compose + ["pull", "agentibridge"])
    if result.returncode != 0:
        print("[docker] ERROR: Failed to pull latest image")
        return

    # Compare digests
    new_digest = (
        subprocess.run(
            ["docker", "images", "--digests", "--no-trunc", "--format", "{{.Digest}}", "ghcr.io/the-cloud-clockwork/agentibridge"],
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .split("\n")[0]
    )

    if old_digest and old_digest == new_digest:
        print(f"[docker] Image already up to date ({_short_digest(old_digest)})")
    elif old_digest:
        print(f"[docker] Image updated: {_short_digest(old_digest)} -> {_short_digest(new_digest)}")
    else:
        print(f"[docker] Image pulled: {_short_digest(new_digest)}")

    # Recreate only agentibridge (preserves redis/postgres data)
    state = _detect_stack_state(stack_dir)
    if state in ("running", "partial"):
        print("[docker] Recreating agentibridge container...")
        subprocess.run(compose + ["up", "-d", "--no-deps", "--force-recreate", "agentibridge"], check=True)
        print()
        subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "name=agentibridge",
                "--format",
                _DOCKER_PS_FORMAT,
            ],
            check=False,
        )
    else:
        print("[docker] Stack is not running. Start it with: agentibridge run")


def _short_digest(digest: str) -> str:
    """Shorten a docker digest for display (e.g. sha256:abc123... -> sha256:abc123)."""
    if not digest or digest == "<none>":
        return "(none)"
    if ":" in digest:
        algo, _, h = digest.partition(":")
        return f"{algo}:{h[:12]}"
    return digest[:12]


def cmd_stop(args: argparse.Namespace) -> None:
    for unit in ("agentibridge", "agentibridge-db"):
        subprocess.run(["systemctl", "--user", "stop", unit], check=False)
    print("AgentiBridge stopped. Start with: agentibridge install")


def cmd_restart(args: argparse.Namespace) -> None:
    subprocess.run(["systemctl", "--user", "restart", "agentibridge-db"], check=False)
    subprocess.run(["systemctl", "--user", "restart", "agentibridge"], check=False)
    print("AgentiBridge restarted.")


def cmd_logs(args: argparse.Namespace) -> None:
    cmd = ["journalctl", "--user", "-u", "agentibridge", "--no-pager"]
    cmd += ["-n", str(args.tail)]
    if args.follow:
        cmd.append("-f")
    subprocess.run(cmd, check=False)


# ---------------------------------------------------------------------------
# Bridge command
# ---------------------------------------------------------------------------


def _read_env_value(key: str, env_file: Path) -> str | None:
    """Parse a single value from a .env file (skips comments)."""
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


def _load_state() -> dict:
    """Read persistent state from ~/.agentibridge/state.json."""
    try:
        return json.loads(_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(data: dict) -> None:
    """Atomically merge *data* into ~/.agentibridge/state.json."""
    _STACK_DIR.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    state.update(data)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fd, tmp = tempfile.mkstemp(dir=str(_STACK_DIR), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.rename(tmp, str(_STATE_FILE))
    except BaseException:
        os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agentibridge",
        description="AgentiBridge — Claude CLI Transcript MCP Server",
    )
    subparsers = parser.add_subparsers(dest="command")

    # init
    subparsers.add_parser("init", help="Initialize ~/.agentibridge/ and verify prerequisites")

    # update
    update_parser = subparsers.add_parser("update", help="Update agentibridge to the latest version")
    update_parser.add_argument("--docker", action="store_true", help="Also update Docker stack even if not running")

    # stop
    subparsers.add_parser("stop", help="Stop agentibridge + databases")

    # restart
    subparsers.add_parser("restart", help="Restart agentibridge + databases")

    # logs
    logs_parser = subparsers.add_parser("logs", help="View agentibridge logs")
    logs_parser.add_argument(
        "--tail", type=int, default=100, metavar="N", help="Number of lines to show (default: 100)"
    )
    logs_parser.add_argument("--follow", "-f", action="store_true", help="Follow log output")

    # version
    subparsers.add_parser("version", help="Print version")

    # status
    subparsers.add_parser("status", help="Check service status and connectivity")

    # help
    subparsers.add_parser("help", help="Show available tools and configuration")

    # connect
    connect_parser = subparsers.add_parser("connect", help="Show connection strings for MCP clients")
    connect_parser.add_argument("--host", default=None, help="Server host (default: localhost)")
    connect_parser.add_argument("--port", default=None, help="Server port (default: 8100)")
    connect_parser.add_argument("--api-key", default=None, help="API key to include in examples")

    # tunnel
    tunnel_parser = subparsers.add_parser("tunnel", help="Cloudflare Tunnel status and named tunnel setup")
    tunnel_parser.add_argument("action", nargs="?", default="status", choices=["status", "setup"])

    # config
    config_parser = subparsers.add_parser("config", help="Show current config or generate .env template")
    config_parser.add_argument("--generate-env", action="store_true", help="Print .env template")

    # install
    serve_parser = subparsers.add_parser("serve", help="Run the MCP server (stdio for Claude Code, sse for HTTP)")
    serve_group = serve_parser.add_mutually_exclusive_group()
    serve_group.add_argument("--stdio", action="store_true", help="Run in stdio mode (default — for Claude Code)")
    serve_group.add_argument("--sse", action="store_true", help="Run in SSE/HTTP mode")

    subparsers.add_parser("install", help="Install as systemd user service")

    # uninstall
    subparsers.add_parser("uninstall", help="Remove systemd service")

    # locks
    locks_parser = subparsers.add_parser("locks", help="Show Redis keys, file locks, and bridge resource state")
    locks_parser.add_argument("--clear", action="store_true", help="Clear all position locks (forces re-index)")

    # search
    search_parser = subparsers.add_parser(
        "search",
        help='Headless recon: spawn claude -p with a recon prompt wrapped around "<query>"',
    )
    search_parser.add_argument("query", nargs="+", help="Query string (quote it)")
    search_parser.add_argument("--model", default="opus", help="Model (default: opus)")
    search_parser.add_argument("--timeout", type=int, default=300, help="Timeout seconds (default: 300)")
    search_parser.add_argument("--instructions", default="", help="Extra instructions appended to prompt")
    search_parser.add_argument("--raw", action="store_true", help="Print only the agent's result text")
    search_parser.add_argument("--json", action="store_true", help="Print full JSON envelope (machine-readable)")

    # embeddings
    embeddings_parser = subparsers.add_parser("embeddings", help="Show embedding pipeline status")
    embeddings_parser.add_argument(
        "--check-llm", action="store_true", help="Test LLM endpoint with a real embedding request"
    )

    args = parser.parse_args()

    commands = {
        "update": cmd_update,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "logs": cmd_logs,
        "init": cmd_init,
        "version": cmd_version,
        "status": cmd_status,
        "help": cmd_help,
        "connect": cmd_connect,
        "tunnel": cmd_tunnel,
        "config": cmd_config,
        "serve": cmd_serve,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "locks": cmd_locks,
        "embeddings": cmd_embeddings,
        "search": cmd_search,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
