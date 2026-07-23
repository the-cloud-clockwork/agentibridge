# agentibridge

You are connected to **agentibridge** — an MCP server that indexes Claude Code
transcripts across all projects and exposes them as tools. The server registers
under the name `agentibridge-mcp` (or whatever the local install configured);
tool names are namespaced as `mcp__<server>__<tool>`.

> **Availability disclaimer.** The tools below exist **only if the agentibridge
> MCP is available** — i.e. you can see `mcp__agentibridge*` tools in this
> session. If those tools are **not** visible, the MCP is not available (the
> server isn't running or isn't registered); the table below does not apply and
> nothing here is callable. Suggest `agentibridge status` to the operator.

## Tools — what each one does

### Recall & inspect (Phase 1)

| Tool | What it does |
|------|--------------|
| `list_sessions` | Enumerate indexed sessions across all projects, most-recent first (metadata only). |
| `get_session` | Full session: metadata + the **tail** of the transcript (`last_n` recent entries; `0` = all). |
| `get_session_segment` | A positional (`offset`/`limit`) or time-range (`since`/`until`) **window** of one transcript — use for pagination. |
| `get_session_actions` | Histogram of tool calls in a session — "what did this session *do*", not its prose. |
| `search_sessions` | Keyword search across session transcripts. |
| `agent_search` | Reconnaissance search via a headless `claude -p` one-shot over the operator's query. |
| `collect_now` | Force the background collector to scan `~/.claude/projects/` immediately instead of waiting for the poll interval. |

### Semantic search & summary (Phase 2)

| Tool | What it does |
|------|--------------|
| `search_semantic` | Embedding-based semantic search across transcripts. Requires LLM API + Postgres/pgvector configured. |
| `generate_summary` | Produce a 2–3 sentence AI summary of a session's transcript. |

### Knowledge catalog (Phase 5)

| Tool | What it does |
|------|--------------|
| `list_memory_files` | List the operator's per-project memory markdown files. |
| `get_memory_file` | Read one memory file. |
| `list_plans` | List plan blueprints. |
| `get_plan` | Read one plan. |
| `search_history` | Search **every prompt the operator has ever submitted** across all projects. |

### A2A agent registry (Phase 6)

| Tool | What it does |
|------|--------------|
| `register_agent` | Register an agent into the A2A registry. |
| `deregister_agent` | Remove a registered agent. |
| `heartbeat_agent` | Keep a registered agent marked online. |
| `list_agents` | List all agents (registered + local, merged). |
| `get_agent` | Fetch one agent record. |
| `find_agents` | Find agents advertising a given capability. |
| `discover_local_agents` | Enumerate session-gated local agents (AgentiHub packages on disk). |
| `run_agent` | Route a task to a specific agent (local agents are always callable — cold-start on dispatch). |
| `dispatch_to_agent` | Route a task to the best available agent for a given capability. |

### Cross-project handoff

| Tool | What it does |
|------|--------------|
| `list_handoff_projects` | List projects under `~/.claude/projects/` with session counts. |
| `handoff` | Seed a new resumable Claude session in a target project with structured context; returns a `session_id` + `resume_command`. |

## When to reach for it

- The operator references prior work ("like we did last week", "the plan we
  wrote for X") → `search_semantic` / `search_history` / `search_sessions`
  before guessing.
- The operator asks to continue work in another repo → `/session-handoff`
  (wraps `handoff` with a fallback memory-file path for when the MCP isn't
  reachable).
- The operator asks "what agents are available" or wants to route work to a
  capability → `find_agents` / `discover_local_agents` / `dispatch_to_agent`.

If `mcp__agentibridge*` tools are not visible, the server isn't running —
suggest `agentibridge status` to the operator.
