# agentibridge

An MCP server indexing Claude Code transcripts across all projects, namespaced
`mcp__agentibridge__<tool>`. The tool list itself is injected into your session
— read the names and schemas there, not here. If no `mcp__agentibridge__*`
tools are visible, the server is not running: suggest `agentibridge status` to
the operator.

## When to reach for it

- The operator references prior work ("like we did last week", "the plan we
  wrote for X") → `search_semantic` / `search_history` / `search_sessions`
  before guessing. `search_history` covers every prompt the operator has ever
  submitted, across all projects.
- The operator asks to continue work in another repo → the `/session-handoff`
  skill, which wraps `handoff` with a memory-file fallback for when the MCP is
  unreachable.
- "What agents are available", or routing work to a capability →
  `find_agents` / `discover_local_agents` / `dispatch_to_agent`. Local agents
  are always callable and cold-start on dispatch.
- Reconstructing what a past session *did* rather than what it said →
  `get_session_actions` (tool-call histogram), then `get_session_segment` to
  page a window of the transcript.
