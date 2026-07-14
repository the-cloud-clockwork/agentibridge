---
title: Configuration
nav_order: 2
parent: Reference
---

# Configuration Reference

This document provides a comprehensive reference for all AgentiBridge configuration options.

## Config File Locations

Each run mode has its own config file in `~/.agentibridge/`:

| File | Purpose | Created by |
|------|---------|------------|
| `~/.agentibridge/.env` | Native mode config | Auto-created on first `import agentibridge` |
| `~/.agentibridge/agentibridge.env` | Docker mode config | Auto-created on first `agentibridge install` |

**Native mode config resolution** (first found wins, explicit env vars always override):

1. Explicit env vars (already set in shell/process)
2. Project-local `.env` (current working directory)
3. `~/.agentibridge/.env` (canonical user config home)

## Run Modes and Storage

AgentiBridge has two run modes that can run simultaneously without conflict:

| Mode | Command | Config file | Storage | Setup |
|------|---------|-------------|---------|-------|
| **Docker** | `agentibridge install` | `agentibridge.env` | Redis + Postgres (bundled) | Zero config — compose starts all 3 containers |
| **Native** | `python -m agentibridge` | `.env` | Filesystem only (default) | No external services needed |

**Docker mode** uses `~/.agentibridge/agentibridge.env` (transport=sse, Redis, Postgres). The collector daemon indexes transcripts into Redis in the background. Tool calls read from Redis — fast, paginated, with time-range filters.

**Native mode** uses `~/.agentibridge/.env` (transport=stdio, no Redis by default). Every tool call (`list_sessions`, `search_sessions`, `get_session`, etc.) reads and parses the raw JSONL files from `~/.claude/projects/` directly. This works but is slower and re-parses files on every call. To add Redis in native mode, run your own instance and set `REDIS_URL` in `.env`.

## Environment Variables

### Storage — Redis

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | _(none)_ | Redis connection URL (e.g., `redis://localhost:6379/0`). When set, sessions are indexed in Redis for fast access. When unset, every tool call re-parses raw JSONL files from disk. In Docker mode this is set automatically. |
| `REDIS_KEY_PREFIX` | `agentibridge` | Namespace prefix for all Redis keys (format: `{prefix}:sb:{key}`) |

### Transport Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIBRIDGE_TRANSPORT` | `stdio` | Transport mode: `stdio` (local MCP via stdin/stdout) or `sse` (HTTP/SSE for remote clients) |
| `AGENTIBRIDGE_HOST` | `127.0.0.1` | Bind address for SSE transport. Use `0.0.0.0` to accept connections from any interface |
| `AGENTIBRIDGE_PORT` | `8100` | HTTP port for SSE transport |
| `AGENTIBRIDGE_API_KEYS` | _(none)_ | Comma-separated list of API keys for authentication. Empty = no auth required |

### Collector Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIBRIDGE_POLL_INTERVAL` | `60` | How often the collector scans for new transcript data (seconds). Minimum: 5 |
| `AGENTIBRIDGE_MAX_ENTRIES` | `500` | Maximum transcript entries to store per session in Redis. `0` = unlimited (use with caution) |
| `CLAUDE_CODE_HOME_DIR` | `~/.claude` | Claude Code home directory. All paths (projects, plans, history, memory) derive from this |

### Storage — Postgres + pgvector (semantic search)

Required for the `search_semantic` and `generate_summary` tools. Without Postgres, keyword search (`search_sessions`) still works. In Docker mode, Postgres is bundled (`pgvector/pgvector:pg16` image). In native mode, run your own Postgres with pgvector and set the URL below.

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIBRIDGE_EMBEDDING_ENABLED` | `false` | Enable semantic search. Requires `POSTGRES_URL` and LLM embedding config. Must be explicitly set to `true` |
| `POSTGRES_URL` | _(none)_ | PostgreSQL connection URL with pgvector extension (e.g., `postgresql://user:pass@localhost:5432/agentibridge`). Also accepted as `DATABASE_URL`. In Docker mode this is set automatically. |
| `PGVECTOR_DIMENSIONS` | `1536` | Embedding vector dimensions. Must match your embedding model (e.g., 1536 for `text-embedding-3-small`) |

### LLM Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | _(none)_ | Anthropic API key for session summary generation (preferred). Uses Claude via official SDK |
| `ANTHROPIC_AUTH_TOKEN` | _(none)_ | Auth token for LLM proxies (LiteLLM, OpenRouter, etc.). Alternative to `ANTHROPIC_API_KEY` — the SDK resolves both automatically |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Base URL for the Anthropic SDK. Set when using an LLM proxy alongside `ANTHROPIC_AUTH_TOKEN` |
| `LLM_API_BASE` | _(none)_ | OpenAI-compatible API base URL for embeddings and chat (e.g., `http://localhost:11434/v1` for Ollama) |
| `LLM_API_KEY` | _(none)_ | API key for the LLM endpoint |
| `LLM_EMBED_MODEL` | _(none)_ | Embedding model name (e.g., `text-embedding-3-small`, `mxbai-embed-large`) |
| `LLM_CHAT_MODEL` | _(none)_ | Chat model for summaries if `ANTHROPIC_API_KEY` is not set (e.g., `gpt-4o-mini`, `llama3`) |
| `CF_ACCESS_CLIENT_ID` | _(none)_ | Cloudflare Access service-token ID. Only needed when your LLM proxy is behind Cloudflare Access Zero Trust (not an AgentiBridge feature — see [Cloudflare Tunnel Guide](../deployment/cloudflare-tunnel.md#fix-3--cloudflare-access-service-token-for-llm-backend-behind-access)) |
| `CF_ACCESS_CLIENT_SECRET` | _(none)_ | Cloudflare Access service-token secret. Paired with `CF_ACCESS_CLIENT_ID` for outbound LLM API requests through Cloudflare Access |

### OAuth 2.1 Configuration (Optional)

AgentiBridge supports OAuth 2.1 for MCP clients that require it (e.g., claude.ai). Set `OAUTH_ISSUER_URL` to enable.

| Variable | Default | Description |
|----------|---------|-------------|
| `OAUTH_ISSUER_URL` | _(none)_ | OAuth issuer URL. **Setting this enables OAuth 2.1.** Example: `https://bridge.example.com` |
| `OAUTH_RESOURCE_URL` | `{issuer}/mcp` | OAuth resource server URL. Defaults to `{OAUTH_ISSUER_URL}/mcp` |
| `OAUTH_CLIENT_ID` | _(none)_ | Pre-configured client ID. When set, disables dynamic client registration |
| `OAUTH_CLIENT_SECRET` | _(none)_ | Pre-configured client secret. Required when `OAUTH_CLIENT_ID` is set |
| `OAUTH_ALLOWED_REDIRECT_URIS` | _(none)_ | Comma-separated allowed redirect URIs for the pre-configured client |
| `OAUTH_ALLOWED_SCOPES` | _(none)_ | Space-separated OAuth scopes the client may request (e.g., `claudeai`) |

When OAuth is enabled, `AGENTIBRIDGE_API_KEYS` still works as a fallback — Bearer tokens matching an API key are accepted.

### Dispatch Configuration (Phase 4)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_BINARY` | `claude` | Path to Claude Code CLI binary. Use absolute path if not in `$PATH` |
| `CLAUDE_DISPATCH_MODEL` | `sonnet` | Model to use for dispatched tasks. Options: `sonnet`, `opus`, `haiku` |
| `CLAUDE_DISPATCH_TIMEOUT` | `300` | Maximum execution time for dispatched tasks (seconds) |
| `CLAUDE_DISPATCH_URL` | _(none)_ | Bridge URL for Docker mode (e.g., `http://host.docker.internal:8101`). Empty = local subprocess mode |
| `DISPATCH_SECRET` | _(none)_ | Shared secret sent from the container to the dispatch (native) |
| `DISPATCH_BRIDGE_HOST` | `0.0.0.0` | Bind address for the host-side dispatch (native) |
| `DISPATCH_BRIDGE_PORT` | `8101` | Port for the host-side dispatch (native) |

### Knowledge Catalog Configuration (Phase 5)

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIBRIDGE_MAX_HISTORY_ENTRIES` | `5000` | Maximum history entries to store in Redis. `0` = unlimited |
| `AGENTIBRIDGE_MAX_MEMORY_CONTENT` | `51200` | Maximum bytes to read from a single memory file (50KB) |
| `AGENTIBRIDGE_MAX_PLAN_CONTENT` | `102400` | Maximum bytes to read from a single plan file (100KB) |

### Local Agents Configuration (Phase 6)

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTIBRIDGE_LOCAL_AGENTS_ENABLED` | `false` | Master toggle for session-gated local agent discovery + dispatch. Must be explicitly set to `true` |
| `AGENTIHUB_DIR` | _(none)_ | AgentiHub checkout root containing `agents/`. Empty = auto-resolve by walking up for a sibling `agentihub/agents` directory |
| `AGENTIBRIDGE_LOCAL_SESSION_TTL` | `3600` | Freshness window (seconds) for a local agent to count as "online" |

**What enabling local agents does.** With the flag on, `list_agents` / `get_agent` / `find_agents` additionally scan `<AGENTIHUB_DIR>/agents/<name>/package/` for `CLAUDE.md` packages and merge them in transparently alongside registered HTTP agents — nothing is persisted to Redis or the file store, the catalog is computed fresh on every read. Each package's capability tags come from its `command.yml`. Liveness is session-gated, not heartbeat-gated: a package is `online` only while a live Claude session's working directory maps to it within `AGENTIBRIDGE_LOCAL_SESSION_TTL`, otherwise it's `idle`. Local agents are never `offline` — they are always callable, because dispatch cold-starts a fresh `claude` process in the package directory regardless of whether a session is currently live there.

### Logging Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_HOOK_LOG_ENABLED` | `true` | Enable or disable structured JSON logging |
| `AGENTIBRIDGE_LOG_FILE` | _auto_ | Log file path. Auto-detects: `/app/logs/agentibridge.log` (Docker) or `~/.cache/agentibridge/agentibridge.log` (native) |

## Configuration Profiles

### Minimal Setup (Local Only)

```bash
# No configuration needed - just run:
docker compose up -d
```

Uses defaults: Redis on `redis://redis:6379/0`, HTTP on `localhost:8100`, no authentication.

### Remote Access Setup

```bash
AGENTIBRIDGE_TRANSPORT=sse
AGENTIBRIDGE_HOST=0.0.0.0
AGENTIBRIDGE_PORT=8100
AGENTIBRIDGE_API_KEYS=secret-key-1,secret-key-2
```

Enables HTTP/SSE transport with API key authentication for remote MCP clients.

### Semantic Search Setup (Phase 2)

```bash
# Enable semantic search (required opt-in)
AGENTIBRIDGE_EMBEDDING_ENABLED=true

# Database
POSTGRES_URL=postgresql://agentibridge:password@localhost:5432/agentibridge
PGVECTOR_DIMENSIONS=1536

# Embeddings
LLM_API_BASE=http://localhost:11434/v1
LLM_EMBED_MODEL=text-embedding-3-small
```

Enables `search_semantic` tool with vector similarity search. `AGENTIBRIDGE_EMBEDDING_ENABLED=true` must be set explicitly — embeddings are off by default.

### Full Production Setup

```bash
# Redis
REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=agentibridge

# Transport
AGENTIBRIDGE_TRANSPORT=sse
AGENTIBRIDGE_HOST=0.0.0.0
AGENTIBRIDGE_PORT=8100
AGENTIBRIDGE_API_KEYS=prod-key-xyz

# Collector
AGENTIBRIDGE_POLL_INTERVAL=30
AGENTIBRIDGE_MAX_ENTRIES=1000

# Database (semantic search)
AGENTIBRIDGE_EMBEDDING_ENABLED=true
POSTGRES_URL=postgresql://agentibridge:secure-password@postgres:5432/agentibridge
PGVECTOR_DIMENSIONS=1536

# LLM
ANTHROPIC_API_KEY=sk-ant-xxxxx
LLM_API_BASE=http://ollama:11434/v1
LLM_EMBED_MODEL=text-embedding-3-small

# Dispatch
CLAUDE_BINARY=/usr/local/bin/claude
CLAUDE_DISPATCH_MODEL=sonnet
CLAUDE_DISPATCH_TIMEOUT=600

# Knowledge Catalog (Phase 5 — paths derived from CLAUDE_CODE_HOME_DIR)
# AGENTIBRIDGE_MAX_HISTORY_ENTRIES=5000

# Logging
CLAUDE_HOOK_LOG_ENABLED=true
```

## CLI Configuration Commands

### View Current Configuration

```bash
agentibridge config
```

Shows all active configuration values, including defaults.

### Generate .env Template

```bash
agentibridge config --generate-env
```

Creates a `agentibridge.env.example` file with all available options and descriptions.

### Validate Configuration

```bash
agentibridge status
```

Checks service health, Redis connectivity, and session count.

## Redis Key Structure

All Redis keys follow the pattern: `{REDIS_KEY_PREFIX}:sb:{suffix}`

**Common keys:**
- `{prefix}:sb:idx:all` — Sorted set of all session IDs (score = last_update timestamp)
- `{prefix}:sb:idx:project:{encoded}` — Sorted set of session IDs per project
- `{prefix}:sb:session:{id}:meta` — Hash of session metadata fields
- `{prefix}:sb:session:{id}:entries` — List of JSON-serialized transcript entries (capped at `AGENTIBRIDGE_MAX_ENTRIES`)
- `{prefix}:sb:pos:{filepath_hash}` — String: byte offset for incremental transcript reading
- `{prefix}:sb:memory:{project}:{filename}` — Hash: memory file metadata + content
- `{prefix}:sb:idx:memory` — Sorted set: all memory file keys by last modified
- `{prefix}:sb:plan:{codename}` — Hash: plan metadata + content
- `{prefix}:sb:plan:{codename}:agents` — List: agent subplan codenames
- `{prefix}:sb:idx:plans` — Sorted set: all plan codenames by last modified
- `{prefix}:sb:codename:{slug}` — Set: session IDs linked to a plan codename
- `{prefix}:sb:history` — List: JSON-serialized prompt history entries
- `{prefix}:sb:pos:history` — String: byte offset for incremental history parsing

## Docker Compose Overrides

The `docker-compose.yml` reads from `agentibridge.env` (not `.env`). The bundled `agentibridge.env` template sets these defaults:

```bash
REDIS_URL=redis://redis:6379/0
AGENTIBRIDGE_TRANSPORT=sse
AGENTIBRIDGE_HOST=0.0.0.0
AGENTIBRIDGE_PORT=8100
```

Override by editing `~/.agentibridge/agentibridge.env` or exporting variables before running `agentibridge install`.

## See Also

- [Architecture Overview](../architecture/internals.md)
- [Remote Access Setup](../architecture/remote-access.md)
- [Semantic Search](../architecture/semantic-search.md)
