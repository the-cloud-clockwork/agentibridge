"""Configuration for agentibridge."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_home_dir = Path.home() / ".agentibridge"

# Load .env from current working directory if present (dev mode)
_cwd_env = Path.cwd() / ".env"

if _cwd_env.exists():
    load_dotenv(_cwd_env, override=False)


def _env_bool(key: str, default: str = "false") -> bool:
    """Parse env var as boolean. Accepts: true/false, 1/0, yes/no."""
    val = os.getenv(key, default).lower()
    return val in ("true", "1", "yes")


def _env_int(key: str, default: str, *, min_val: Optional[int] = None, max_val: Optional[int] = None) -> int:
    """Parse env var as int with optional bounds validation."""
    val = int(os.getenv(key, default))
    if min_val is not None and val < min_val:
        val = min_val
    if max_val is not None and val > max_val:
        val = max_val
    return val


# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

LOG_ENABLED = _env_bool("CLAUDE_HOOK_LOG_ENABLED", "true")


def _default_log_file() -> str:
    if Path("/.dockerenv").exists():
        return "/app/logs/agentibridge.log"
    return str(Path.home() / ".cache" / "agentibridge" / "agentibridge.log")


LOG_FILE = os.getenv("AGENTIBRIDGE_LOG_FILE", _default_log_file())

# =============================================================================
# AGENTIBRIDGE CONFIGURATION
# =============================================================================

# Enable/disable agentibridge collector background polling
AGENTIBRIDGE_ENABLED = _env_bool("AGENTIBRIDGE_ENABLED", "true")

# Polling interval in seconds (minimum 5s)
AGENTIBRIDGE_POLL_INTERVAL = _env_int("AGENTIBRIDGE_POLL_INTERVAL", "60", min_val=5)

# Claude Code home directory — single root for all paths
CLAUDE_CODE_HOME_DIR = os.getenv(
    "CLAUDE_CODE_HOME_DIR",
    str(Path.home() / ".claude"),
)

# Derived paths — all relative to CLAUDE_CODE_HOME_DIR
AGENTIBRIDGE_PROJECTS_DIR = str(Path(CLAUDE_CODE_HOME_DIR) / "projects")

# Maximum entries to store per session in Redis (0 = unlimited)
AGENTIBRIDGE_MAX_ENTRIES = _env_int("AGENTIBRIDGE_MAX_ENTRIES", "500", min_val=0)

# =============================================================================
# AGENTIBRIDGE — SEMANTIC SEARCH (Phase 2)
# =============================================================================

# Embedding enabled flag for semantic search
AGENTIBRIDGE_EMBEDDING_ENABLED = _env_bool("AGENTIBRIDGE_EMBEDDING_ENABLED", "false")

# Postgres connection URL for vector storage (pgvector)
POSTGRES_URL = os.getenv("POSTGRES_URL", os.getenv("DATABASE_URL", ""))

# Embedding vector dimensions (must match model: text-embedding-3-small=1536)
PGVECTOR_DIMENSIONS = _env_int("PGVECTOR_DIMENSIONS", "1536", min_val=1, max_val=4096)

# =============================================================================
# AGENTIBRIDGE — REMOTE ACCESS (Phase 3)
# =============================================================================

# Transport mode: "stdio" (local MCP, default) or "sse" (HTTP remote)
AGENTIBRIDGE_TRANSPORT = os.getenv("AGENTIBRIDGE_TRANSPORT", "stdio")

# Port for SSE/HTTP transport (1-65535)
AGENTIBRIDGE_PORT = _env_int("AGENTIBRIDGE_PORT", "8100", min_val=1, max_val=65535)

# Host for SSE/HTTP transport
AGENTIBRIDGE_HOST = os.getenv("AGENTIBRIDGE_HOST", "127.0.0.1")

# Comma-separated API keys for remote access auth
AGENTIBRIDGE_API_KEYS = os.getenv("AGENTIBRIDGE_API_KEYS", "")

# Comma-separated tool names to remove from the server (empty = all tools enabled)
AGENTIBRIDGE_REMOVE_TOOLS: list[str] = [
    t.strip() for t in os.getenv("AGENTIBRIDGE_REMOVE_TOOLS", "").split(",") if t.strip()
]

# =============================================================================
# AGENTIBRIDGE — OAUTH 2.1 (opt-in)
# =============================================================================

# OAuth issuer URL — enables OAuth 2.1 when set (e.g., https://homebridge.example.com)
OAUTH_ISSUER_URL = os.getenv("OAUTH_ISSUER_URL", "")

# OAuth resource server URL — defaults to {OAUTH_ISSUER_URL}/mcp
OAUTH_RESOURCE_URL = os.getenv("OAUTH_RESOURCE_URL", "")

# Pre-configured OAuth client credentials — disables dynamic registration when set
OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "")

# Comma-separated allowed OAuth redirect URIs (required for pre-configured clients)
OAUTH_ALLOWED_REDIRECT_URIS = os.getenv("OAUTH_ALLOWED_REDIRECT_URIS", "")

# Space-separated OAuth scopes the client is allowed to request (e.g. "claudeai")
OAUTH_ALLOWED_SCOPES = os.getenv("OAUTH_ALLOWED_SCOPES", "")

# =============================================================================
# AGENTIBRIDGE — KNOWLEDGE CATALOG (Phase 5)
# =============================================================================

AGENTIBRIDGE_PLANS_DIR = str(Path(CLAUDE_CODE_HOME_DIR) / "plans")

AGENTIBRIDGE_HISTORY_FILE = str(Path(CLAUDE_CODE_HOME_DIR) / "history.jsonl")

AGENTIBRIDGE_MAX_HISTORY_ENTRIES = _env_int("AGENTIBRIDGE_MAX_HISTORY_ENTRIES", "5000", min_val=0)

AGENTIBRIDGE_MAX_MEMORY_CONTENT = _env_int("AGENTIBRIDGE_MAX_MEMORY_CONTENT", "51200", min_val=1024)

AGENTIBRIDGE_MAX_PLAN_CONTENT = _env_int("AGENTIBRIDGE_MAX_PLAN_CONTENT", "102400", min_val=1024)

# =============================================================================
# AGENTIBRIDGE — LOCAL AGENTS (Phase 6)
# =============================================================================
# Session-gated local agent discovery. A "local agent" is a purpose-built
# Claude Code package on disk under an AgentiHub checkout
# (``<AGENTIHUB_DIR>/agents/<name>/package/CLAUDE.md``). Unlike service agents
# (HTTP endpoint + heartbeat), local agents are discovered by scanning the
# filesystem and their liveness is derived from whether a live ``claude``
# session is running in the package directory.

# Master toggle for local agent discovery + dispatch. Default off — existing
# deployments see no behavior change until this is explicitly enabled.
AGENTIBRIDGE_LOCAL_AGENTS_ENABLED = _env_bool("AGENTIBRIDGE_LOCAL_AGENTS_ENABLED", "false")

# AgentiHub checkout root (contains an ``agents/`` subdir). Empty = auto-resolve
# via sibling-directory discovery. Same "empty means unset" convention as
# OAUTH_ISSUER_URL — no universal default location, so never guessed here.
AGENTIHUB_DIR = os.getenv("AGENTIHUB_DIR", "")

# Freshness window (seconds) for calling a local agent "online": a package is
# online only if a claude session in its dir was active within this window.
AGENTIBRIDGE_LOCAL_SESSION_TTL = _env_int("AGENTIBRIDGE_LOCAL_SESSION_TTL", "3600", min_val=1)
