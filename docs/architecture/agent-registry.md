---
title: Agent Registry
nav_order: 6
parent: Architecture
---

# Agent Registry (A2A Discovery)

Agent-to-agent discovery and dispatch (Phase 6). Any agent can announce itself, discover peers, and route a task to a peer by id or by capability — either a **registered HTTP agent** (self-registers, sends heartbeats) or a **local agent** (a Claude Code package on disk, discovered by scanning the filesystem, never persisted).

## Architecture

```
                    register_agent() / heartbeat_agent()
                                  │
                                  v
                    ┌─────────────────────────┐
                    │   registry.py           │
                    │   Redis (primary)        │
                    │   + file fallback        │
                    └────────────┬─────────────┘
                                 │
                    list_agents() / get_agent() / find_agents()
                                 │
                                 v
                    ┌─────────────────────────┐        filesystem scan
                    │  merge computed local    │◄────── (no Redis, no file
                    │  agents (local_agents.py)│         write — read-time
                    └────────────┬─────────────┘         only)
                                 │
                run_agent() / dispatch_to_agent()
                                 │
                    ┌────────────┴────────────┐
                    │                         │
              transport="http"          transport="local"
                    │                         │
                    v                         v
          POST {endpoint}/jobs      spawn `claude` in
          (must be online)          metadata.package_path
                                     (cold start always allowed)
```

## Two Kinds of Agent

| | Registered (HTTP) agents | Local agents |
|---|---|---|
| **Discovery** | Self-register on boot, explicit `register_agent()` call | Computed at read time — filesystem scan of `<AGENTIHUB_DIR>/agents/<name>/package/CLAUDE.md` |
| **Persistence** | Redis hash + file fallback (`registry.py`) | Never persisted — nothing written to Redis or disk (`local_agents.py`) |
| **Liveness** | Heartbeat-TTL: `last_heartbeat` within `heartbeat_ttl` seconds | Session-gated: a live `claude` session's cwd maps to the package dir within `AGENTIBRIDGE_LOCAL_SESSION_TTL` |
| **Status vocabulary** | `online` / `offline` / `degraded` | `online` / `idle` — **never** `offline` |
| **Dispatch gate** | Must be `online` with `available_capacity > 0` | Always dispatchable — a cold start is always allowed |
| **Transport** | `"http"` — `POST {endpoint}/jobs` | `"local"` — spawn a fresh `claude` in `metadata.package_path` |

### The online/idle-never-offline contract

A local agent is **always callable**. If no live session is running in its package directory, dispatch simply cold-starts a fresh `claude` there — there is no "unreachable" state for a local package the way there is for a dead HTTP pod.

This is why local agents report `idle`, never `offline`, when no session is live:

- **Registered HTTP agents**: `offline` means the pod is down and cannot serve — a real, hard failure. Forwarding a task to an offline HTTP agent will fail.
- **Local agents**: reporting `offline` for "no session running right now" was tried and rejected — it caused LLM callers to hedge or refuse to dispatch valid calls that would, in fact, have succeeded via cold start. `idle` correctly conveys *warmth* (is a session already running there) without implying *reachability* (can I call this at all — yes, always).

Every local-agent record carries `dispatchable: true`, `available_capacity: 1`, and `metadata.cold_start_on_dispatch` (true when idle) so an orchestrator never needs to guess.

## AgentRecord Schema

```python
@dataclass
class AgentRecord:
    agent_id: str
    agent_name: str = ""
    agent_type: str = ""
    capabilities: list = field(default_factory=list)
    endpoint: str = ""
    transport: str = "http"        # "http" (default) or "local"
    status: str = "online"
    metadata: dict = field(default_factory=dict)
    registered_at: str = ""
    last_heartbeat: str = ""
    heartbeat_ttl: int = 300
```

`transport` is the field that determines dispatch behavior in `route_to_agent`:

- `"http"` (default) — deliver by `POST {endpoint}/jobs`. Requires `effective_status == "online"` and `available_capacity > 0`.
- `"local"` — deliver by spawning `claude` in `metadata.package_path`. Liveness is advisory only; a cold start is always allowed.

Local agent records are shaped as `AgentRecord`-compatible dicts (`transport="local"`, `endpoint=""`, `metadata.package_path` set) so they flow through `list_agents` / `get_agent` / `find_agents` unchanged — callers don't need to special-case them.

## Capability Tags

Registered agents declare `capabilities` explicitly at `register_agent()` time (e.g. `"profile:coding"`, `"agent_mode"`).

Local agents derive theirs from the package's `command.yml`:

```yaml
name: video-editor-agent      # canonical package id — read by agentihooks install.py,
                               # NOT a display label
description: Edits and assembles video from raw footage
capabilities:
  - video-editing
  - content-drafting
```

`read_package_manifest()` parses `command.yml`'s `name`, `description`, and `capabilities` keys. Base identity tags (`"local"`, `f"agent:{name}"`) are always present; declared domain tags are appended, order-preserving and deduped. A missing, unreadable, or malformed `command.yml` is best-effort — the agent stays discoverable with just its base tags, never breaks discovery.

`name` is the canonical package id used by agentihooks' `install.py` — treat it as an identifier, not prose.

`find_agents(capability=...)` and `dispatch_to_agent(capability=...)` route on these real domain tags — `cost-analysis`, `video-editing`, `content-drafting`, etc. — not on transport or agent type.

## Routing

### `route_to_agent` (by agent_id)

1. `get_agent(agent_id)` — checks registered (Redis/file) first, falls back to a computed local-agent lookup.
2. If `transport == "local"` — branch immediately to `_route_to_local_agent()`, **before** the online/capacity/endpoint checks. Liveness is advisory for discovery only, never a gate on direct dispatch by id.
3. Otherwise (HTTP) — require `effective_status == "online"` and `available_capacity > 0`, then `POST {endpoint}/jobs`.

### `route_by_capability` (best-match)

1. `find_agents(capability)` — merges registered + local candidates.
2. Candidate filter: HTTP agents must be `online`; local agents are candidates while `idle` too (cold-start covers it).
3. Sort: warm (`online`) agents preferred, then by `available_capacity`.
4. Capacity gate applies to HTTP agents only — a local cold start has no queue to be at capacity.
5. Forward via `route_to_agent` with the winning `agent_id`.

## Security Model

Local dispatch (`_route_to_local_agent`) is gated by two independent checks, both mandatory:

1. **Feature flag** — hard-gated on `AGENTIBRIDGE_LOCAL_AGENTS_ENABLED`. A persisted `transport="local"` record cannot dispatch while the flag is off, even if one exists in Redis/file.
2. **Re-derived `package_path`** — the directory `claude` is spawned in is **never** taken from a persisted record's `metadata.package_path`. It is re-resolved from the live filesystem scan via `local_agents.get_local_agent(agent_id)`, which:
   - only accepts a single, traversal-free path component (`_safe_agent_id` rejects `.`, `..`, `/`, `\\`),
   - only returns a record for `<hub>/agents/<id>/package` when it actually contains `CLAUDE.md`,
   - defense-in-depth containment check: the resolved package path must be a descendant of the resolved AgentiHub root.

This closes off a forged or stale registry entry from ever running `claude` in an arbitrary host directory — the filesystem is the source of truth for *where*, not the record.

## Redis Keys

```
agentibridge:sb:agent:{agent_id}                  # Hash: full AgentRecord fields
agentibridge:sb:idx:agents                        # Sorted set: all agent ids (score = last_heartbeat)
agentibridge:sb:idx:agents:type:{agent_type}       # Sorted set: agent ids by agent_type
agentibridge:sb:idx:agents:cap:{capability}        # Set: agent ids advertising a capability
```

File fallback: `/tmp/agentibridge_agents/{agent_id}.json`, one file per registered agent. Local agents have **no** Redis keys and **no** files — `discover_local_agents()` / `get_local_agent()` are pure filesystem-scan + session-store reads, computed fresh on every call.

## MCP Tools

### `register_agent`

Register (or upsert) an agent for A2A discovery. Idempotent.

```
Args:
  agent_id       (str)                    — unique agent identifier
  agent_name     (str, optional)          — human-readable name
  agent_type     (str, optional)          — category (e.g. "executor", "specialist")
  capabilities   (str, default "[]")      — JSON array of capability strings
  endpoint       (str, optional)          — URL to reach this agent; empty for local transport
  metadata       (str, default "{}")      — JSON object; include "package_path" for transport="local"
  heartbeat_ttl  (int, default 300)       — seconds before considered offline
  transport      (str, default "http")    — "http" or "local"
```

### `deregister_agent`

Remove an agent from the registry.

```
Args: agent_id (str)
```

### `heartbeat_agent`

Update an agent's heartbeat timestamp and status. Call periodically to signal liveness.

```
Args:
  agent_id  (str)
  status    (str, default "online")   — "online" or "degraded"
  metadata  (str, default "{}")       — merged into existing metadata
```

### `list_agents`

List registered agents with optional filters. Transparently merges in computed local agents when the feature is enabled.

```
Args:
  agent_type  (str, optional)
  capability  (str, optional)
  status      (str, optional)   — "online", "offline", "degraded", "idle"
  limit       (int, default 50)
```

### `get_agent`

Get full details of a single agent by id — registered first, computed local agent as fallback.

```
Args: agent_id (str)
Returns: JSON with agent record including effective_status
```

### `find_agents`

Find agents advertising a specific capability.

```
Args: capability (str)   — e.g. "run_task", "profile:coding", "cost-analysis"
```

### `discover_local_agents`

Discover session-gated local agents (AgentiHub packages) on this host directly, without the registered-agent merge. Requires `AGENTIBRIDGE_LOCAL_AGENTS_ENABLED=true`.

```
Args: status (str, optional)   — filter by "online" or "idle"
Returns: resolved agentihub path, whether the feature is enabled, and the
         list of local agents (with a shadowed_by_registered flag when a
         registered record shares the same agent_id)
```

### `run_agent`

Route a task to a specific agent by id.

```
Args:
  agent_id  (str)              — target agent
  task      (str)              — what the agent should do
  profile   (str, optional)
  repo_url  (str, optional)
  wait      (bool, default false)
  file_path (str, optional)
Returns: JSON with success, agent_id, job details, or error with retry flag
```

### `dispatch_to_agent`

Route a task to the best available agent for a capability.

```
Args:
  capability (str)             — required capability, e.g. "agent:publishing"
  task       (str)
  profile    (str, optional)
  repo_url   (str, optional)
  wait       (bool, default false)
  file_path  (str, optional)
Returns: JSON with success, agent_id, routed_by, job details, or error with retry flag
```

## Configuration

```bash
# Registered (HTTP) agent defaults
# heartbeat_ttl is per-agent, passed at register_agent() call time (default 300s)

# Local agents (Phase 6) — off by default, no behavior change until enabled
AGENTIBRIDGE_LOCAL_AGENTS_ENABLED=false
AGENTIHUB_DIR=                        # empty = auto-resolve via sibling-directory discovery
AGENTIBRIDGE_LOCAL_SESSION_TTL=3600   # seconds a session stays "online" after last activity
```

`AGENTIHUB_DIR` follows the same "empty means unset" convention as `OAUTH_ISSUER_URL` — there is no universal default location, so it is never guessed at import time. When empty, `resolve_agentihub_dir()` walks up from `local_agents.py`'s own location looking for a sibling `agentihub/agents/` directory, mirroring agenticore's 4-tier resolver (minus the state-file tier).

## Dependencies

- `pyyaml` — parses each package's `command.yml` manifest for capability tags.

## See Also

- [Internal Architecture](internals.md) — Full module reference and design patterns
- [Session Dispatch](session-dispatch.md) — `dispatch_task` / `run_claude` internals used by local-agent cold starts
- [Configuration Reference](../reference/configuration.md) — All environment variables
