---
title: Session Dispatch
nav_order: 3
parent: Architecture
---

# Session Dispatch & Context Restore

Session continuity and agent dispatch. Agents can restore context from a
past session for injection into a new conversation, or run a task on a
registered agent (HTTP or local) through `run_agent` / `dispatch_to_agent`.

## MCP Tools

### `restore_session`

Load context from a past session for injection into a new conversation.

```
Args: session_id (str), last_n (int, default 20)
Returns: JSON with formatted context string + char_count
```

### `run_agent` / `dispatch_to_agent`

Route a task to a specific agent, or to the best match by capability (see
[Agent Registry](agent-registry.md)). An HTTP agent's job is submitted to
its own REST API and polled through that API. A local agent always runs to
completion via `run_claude` — there is no background job id to poll.

## Claude CLI Runner

`agentibridge/claude_runner.py` runs the `claude` CLI directly via
subprocess for every caller that needs a one-shot Claude run: `agent_search`,
`run_agent` / `dispatch_to_agent` against a local agent, and
`init-interactive-agent`.

## Permissions

Claude CLI invocations spawned this way run with `--dangerously-skip-permissions`.
No approval prompts will block them.

## Command Presets

`run_claude` takes a `model` directly (default from `CLAUDE_DISPATCH_MODEL`,
`sonnet`); callers pick the model themselves rather than through a preset
name.

## Dispatch Bridge (Docker Mode)

When AgentiBridge runs inside Docker, the `claude` CLI binary isn't available
in the container. A lightweight HTTP bridge runs on the **host** and proxies
every `run_claude()` call to the local CLI, whichever MCP tool triggered it.

```
┌─────────────────────┐        HTTP         ┌──────────────────────┐
│ Docker container     │◄──────────────────►│  Host bridge          │
│ AgentiBridge MCP     │   POST /dispatch    │  dispatch_bridge.py   │
│                      │   → 202 + job_id    │                      │
│ claude_runner.py     │                     │  Spawns claude CLI    │
│  _run_claude_http()  │   GET /job/{id}     │  Returns result       │
│  submit + poll       │   → job state       │                      │
└─────────────────────┘                     └──────────────────────┘
```

### Submit + Poll Pattern

The bridge is **fire-and-forget**: `POST /dispatch` validates the request,
spawns a background task, and returns HTTP 202 with a `job_id` immediately.
The client polls `GET /job/{id}` with exponential backoff (2s → 10s cap)
until the job finishes or the deadline is exceeded. This job id is internal
to the bridge — it never reaches an MCP tool caller, who only sees the final
result once `run_claude()` returns.

### Bridge Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | No | Health check |
| `POST` | `/dispatch` | `X-Dispatch-Secret` | Submit a dispatch request, returns 202 |
| `GET` | `/job/{id}` | No | Get job status and result |
| `GET` | `/jobs` | No | List all bridge-level job summaries |

### Recursion Prevention

The bridge clears `CLAUDE_DISPATCH_URL` from its environment before calling `run_claude()`, preventing the CLI runner from routing back to the bridge (which would cause infinite recursion).

## Components

| File | Role |
|------|------|
| `agentibridge/dispatch.py` | Session context restore |
| `agentibridge/claude_runner.py` | Claude CLI subprocess / HTTP bridge proxy (submit + poll) |
| `agentibridge/dispatch_bridge.py` | Host-side HTTP bridge (Docker mode), fire-and-forget |

## Configuration

```bash
CLAUDE_BINARY=claude
CLAUDE_DISPATCH_MODEL=sonnet
CLAUDE_DISPATCH_TIMEOUT=300

# Docker mode — bridge on host proxies CLI calls
# CLAUDE_DISPATCH_URL removed (native dispatch)
DISPATCH_SECRET=<shared-secret>
DISPATCH_BRIDGE_HOST=0.0.0.0
DISPATCH_BRIDGE_PORT=8101
```
