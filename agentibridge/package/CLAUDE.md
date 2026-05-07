# agentibridge

You are connected to **agentibridge** — an MCP server that indexes Claude Code
transcripts across all projects and exposes them as tools. The server registers
under the name `agentibridge-development` (or whatever the local install
configured); tool names are namespaced as `mcp__<server>__<tool>`.

## What you can do with it

- **Recall past work** — `list_sessions`, `get_session`, `get_session_segment`,
  `search_sessions` (keyword), `search_semantic` (embeddings), `search_history`
  (every prompt the operator has ever submitted).
- **Resume context** — `restore_session` extracts a context blob from a past
  session for injection into a new conversation.
- **Read curated knowledge** — `list_memory_files`, `get_memory_file`,
  `list_plans`, `get_plan` reach the operator's per-project memory and plans.
- **Dispatch background work** — `dispatch_task` / `plan_task` /
  `execute_plan` spawn headless `claude` runs and return job IDs;
  `get_dispatch_job` polls them.
- **Cross-project handoff** — `handoff` seeds a new resumable session in
  another project. The `/session-handoff` skill wraps this with the fallback
  memory-file path for when the MCP isn't reachable.
- **A2A registry** — `register_agent`, `heartbeat_agent`, `find_agents`,
  `dispatch_to_agent` for fleet coordination.

## When to reach for it

- The operator references prior work ("like we did last week", "the plan we
  wrote for X") → `search_semantic` or `search_history` before guessing.
- The operator asks to continue work in another repo → `/session-handoff`.
- A task is long-running and parallelisable → `dispatch_task` instead of
  blocking the current session.

If `mcp__agentibridge*` tools are not visible, the server isn't running —
suggest `agentibridge status` to the operator.
