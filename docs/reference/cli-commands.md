---
title: CLI Commands
nav_order: 1
parent: Reference
---

# CLI Command Reference

Complete reference for the `agentibridge` command-line tool.

---

## Service Management

AgentiBridge runs **natively on the host**; only Redis + Postgres run in Docker.

### `agentibridge install`

Install the systemd user services (databases + native app), register the MCP
entry in `~/.claude.json`, and converge the daemon onto the new config.

```
agentibridge install [--transport {stdio,sse}]
```

| Flag | Description |
|------|-------------|
| `--transport stdio` | Register a stdio entry — Claude Code spawns the server per session (default on a fresh box) |
| `--transport sse` | Register a url entry `http://localhost:<port>/sse` pointing at the shared daemon — required where an enterprise policy silently filters stdio MCP servers |

The chosen shape is recorded as `AGENTIBRIDGE_MCP_REGISTRATION` in
`~/.agentibridge/agentibridge.env`; a later plain `agentibridge install`
**keeps** it, so re-installs never silently downgrade an sse registration.

Install ends with an unconditional daemon restart (`ensure_running`) so the
running process always matches the config just written. On machines without a
user systemd session (WSL2 without `systemd=true`) the **pidfile backend**
starts the daemon instead — see `agentibridge daemon` below.

---

### `agentibridge uninstall`

Stop the daemon (both backends), remove the systemd units, deregister the MCP
entry, and **verify** teardown — warning if any daemon process survived.
Config files in `~/.agentibridge/` are not removed.

---

### `agentibridge daemon <start|stop|restart|status>`

Daemon lifecycle for the network (sse) transport. Backend is auto-selected:

| Backend | Selected when | Supervision |
|---------|---------------|-------------|
| `systemd` | a user systemd bus answers `systemctl --user show-environment` | unit restarts on crash, survives reboot |
| `pidfile` | no user bus (e.g. WSL2 without systemd) | none — detached `serve --sse` child dies with the machine; run `agentibridge daemon start` once per boot |

Pidfile-backend state lives in `~/.agentibridge/`: pid in `mcp-daemon.pid`,
logs in `logs/mcp-daemon.log`. Force a backend with
`AGENTIBRIDGE_MCP_SUPERVISOR=systemd|pidfile`. `stop` always tears down both
backends. `restart` is an unconditional stop-then-start convergence.

---

### `agentibridge stop` / `agentibridge restart`

`stop` = daemon stop (both backends) + database unit stop.
`restart` = `daemon restart` — stops whatever runs and starts on the current
config, so env-file changes (port, keys, transport) take effect.

---

### `agentibridge serve`

Run the MCP server in the foreground.

```
agentibridge serve [--stdio | --sse]
```

Loads `$AGENTIBRIDGE_ENV_FILE` (default `~/.agentibridge/agentibridge.env`)
first; the flag overrides any inherited `AGENTIBRIDGE_TRANSPORT`. `--stdio` is
what the registered stdio entry invokes; `--sse` is what the pidfile backend
runs under the hood.

---

### `agentibridge logs`

View service logs (`--follow` to stream): journalctl on systemd machines,
launchd log files on macOS. On the pidfile backend read
`~/.agentibridge/logs/mcp-daemon.log` directly.

---

## Status & Info

### `agentibridge status`

Print a multi-section health report.

```
agentibridge status
```

Sections printed:

| Section | What it checks |
|---------|---------------|
| `[Service]` | `systemctl --user is-active agentibridge` |
| `[Docker Stack]` | Health status of `agentibridge`, `agentibridge-redis`, `agentibridge-postgres` containers |
| `[Redis]` | Ping + indexed session count |
| `[Postgres]` | Connection + chunk/session counts from `transcript_chunks` table |
| `[Tunnel]` | Cloudflare Tunnel container state + quick-tunnel URL (if running) |
| `[Transcripts]` | Path to `~/.claude/projects/` and count of `.jsonl` files |
| `[Config]` | Active transport, port, and poll interval |

---

### `agentibridge version`

Print the installed version.

```
agentibridge version
```

---

### `agentibridge help`

Print available MCP tools, configuration variables, and usage examples.

```
agentibridge help
```

---

### `agentibridge config`

Show the current resolved configuration or generate a `.env` template.

```
agentibridge config [--generate-env]
```

Without flags: prints each known environment variable with its current value and
source (`env` = set in environment, `default` = using built-in default).

**Flags**

| Flag | Description |
|------|-------------|
| `--generate-env` | Print a fully-commented `.env` template to stdout. Redirect to a file to bootstrap a new deployment: `agentibridge config --generate-env > .env` |

---

## Cloudflare Tunnel

### `agentibridge tunnel`

Show Cloudflare Tunnel container state and the active URL.

```
agentibridge tunnel [status]
```

Checks both the `agentibridge-tunnel` Docker container and the `cloudflared` systemd service. Outputs differ by mode:

- **Quick tunnel (Docker)** — prints the `*.trycloudflare.com` URL and a ready-to-paste
  `~/.mcp.json` snippet including an API key (if `AGENTIBRIDGE_API_KEYS` is set).
- **Named tunnel (Docker)** — confirms connected state and directs you to the Cloudflare
  Zero Trust dashboard for the hostname.
- **Systemd service** — shows tunnel ID, hostname, service target, and credentials path
  from `~/.cloudflared/config.yml`, plus a ready-to-paste `~/.mcp.json` snippet.
- **Not running** — prints start instructions for both quick and named tunnel modes.

---

### `agentibridge tunnel setup`

Interactive 10-step wizard to install and configure a named Cloudflare tunnel.

```
agentibridge tunnel setup
```

Steps:

| # | Action |
|---|--------|
| 1 | Install `cloudflared` if not already present (Linux amd64/arm64/arm via direct binary, macOS via Homebrew) |
| 2 | Authenticate with Cloudflare (`cloudflared tunnel login`) if not already logged in |
| 3 | Prompt for tunnel name (default: `agentibridge`) |
| 4 | Create the tunnel if it does not already exist (idempotent) |
| 5 | Prompt for subdomain (e.g. `mcp`) |
| 6 | Prompt for domain (e.g. `example.com`) |
| 7 | Set DNS CNAME route (`cloudflared tunnel route dns`) |
| 8 | Write `~/.cloudflared/config.yml` (backs up any existing file with a timestamp suffix) |
| 9 | Optionally install and enable `cloudflared` as a systemd service (Linux only) |
| 10 | Health check: `curl https://<hostname>/health` |

---

## Client & Service Setup

### `agentibridge connect`

Print connection strings for all supported MCP clients.

```
agentibridge connect [--host HOST] [--port PORT] [--api-key KEY]
```

Outputs ready-to-paste configuration for: Claude Code CLI (`~/.mcp.json`),
ChatGPT Custom GPT Actions, Claude Web (MCP), generic SSE API, and a `curl`
health check.

**Flags**

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `localhost` | Server hostname or IP |
| `--port` | `8100` | Server port |
| `--api-key` | `your-api-key` | API key to embed in examples |

---

### `agentibridge locks`

Inspect Redis keys, file position locks, and running bridge processes.

```
agentibridge locks [--clear]
```

Sections:

| Section | Content |
|---------|---------|
| `[Redis Keys]` | Session index size, project indexes, file position offsets, session data key counts, memory usage |
| `[File Position Locks]` | `.pos` files under `~/.cache/agentibridge/positions/` with byte offsets |
| `[Bridge Processes]` | Live `agentibridge` processes (via `pgrep`) + Docker container list |

**Flags**

| Flag | Description |
|------|-------------|
| `--clear` | Delete all file `.pos` locks and Redis `pos:*` keys, forcing a full re-index on the next collection cycle |

---

### `agentibridge embeddings`

Show the full embedding pipeline status: configuration, LLM backend connectivity, Postgres vector storage stats, and embedding coverage.

```
agentibridge embeddings [--check-llm]
```

Sections:

| Section | Content |
|---------|---------|
| `[Config]` | `AGENTIBRIDGE_EMBEDDING_ENABLED`, `LLM_API_BASE`, `LLM_API_KEY` (redacted), `LLM_EMBED_MODEL`, `PGVECTOR_DIMENSIONS` |
| `[LLM Backend]` | Whether the LLM endpoint is configured; with `--check-llm`, sends a test embedding request |
| `[Postgres]` | Connection status, `transcript_chunks` table existence, total chunks, sessions embedded, table size, last embedded timestamp |
| `[Coverage]` | Total sessions in Redis vs sessions with embeddings, with a coverage percentage |

**Flags**

| Flag | Description |
|------|-------------|
| `--check-llm` | Send a real (tiny) embedding request to the LLM endpoint to verify connectivity. Off by default to avoid API costs/latency |

---

## `agentibridge.env` Required Variables

The following variables are validated by `_validate_env` before every
`run`, `stop`, `restart`, or `logs` invocation. If any are absent the
command exits with a descriptive error. These are checked in `~/.agentibridge/agentibridge.env`.

| Variable | Description |
|----------|-------------|
| `REDIS_URL` | Redis connection URL (e.g. `redis://redis:6379/0`) |
| `POSTGRES_URL` | Postgres connection URL (e.g. `postgresql://user:pass@localhost:5432/db`) |
| `POSTGRES_USER` | Postgres username |
| `POSTGRES_PASSWORD` | Postgres password |
| `POSTGRES_DB` | Postgres database name |
| `AGENTIBRIDGE_TRANSPORT` | Daemon serve mode (`sse` for the shared daemon; the MCP registration shape is tracked separately as `AGENTIBRIDGE_MCP_REGISTRATION`) |
| `AGENTIBRIDGE_PORT` | HTTP port for SSE transport (e.g. `8100`) |

Generate a fully-annotated template:

```bash
agentibridge config --generate-env > ~/.agentibridge/agentibridge.env
```

See [Configuration](configuration.md) for the complete list of optional variables.
