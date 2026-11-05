"""Session restore for AgentiBridge.

Extracts context from past sessions for injection into new conversations.
"""

# ---------------------------------------------------------------------------
# Session context restore
# ---------------------------------------------------------------------------


def restore_session_context(session_id: str, last_n: int = 20) -> str:
    """Extract relevant context from a past session.

    Builds a formatted context string from the session's metadata and
    recent transcript entries, suitable for injection into a new agent
    call's system prompt or context field.

    Args:
        session_id: Session UUID to restore context from
        last_n: Number of recent turns to include

    Returns:
        Formatted context string
    """
    from agentibridge.store import SessionStore

    store = SessionStore()
    meta = store.get_session_meta(session_id)
    if not meta:
        raise ValueError(f"Session not found: {session_id}")

    entries = store.get_session_entries(session_id, offset=0, limit=10000)

    # Take last N entries
    if last_n and len(entries) > last_n:
        entries = entries[-last_n:]

    # Build context header
    lines = [
        "=" * 60,
        "RESTORED SESSION CONTEXT",
        "=" * 60,
        f"Session ID: {meta.session_id}",
        f"Project: {meta.project_path}",
        f"Branch: {meta.git_branch}",
        f"Started: {meta.start_time}",
        f"Last Active: {meta.last_update}",
        f"Stats: {meta.num_user_turns} user turns, {meta.num_assistant_turns} assistant turns, {meta.num_tool_calls} tool calls",
    ]

    if meta.summary:
        lines.append(f"Summary: {meta.summary}")

    lines.append("-" * 60)
    lines.append("RECENT CONVERSATION:")
    lines.append("-" * 60)

    # Format entries
    for entry in entries:
        if entry.entry_type == "user":
            lines.append(f"\n[USER] {entry.content[:1000]}")
        elif entry.entry_type == "assistant":
            tools = ""
            if entry.tool_names:
                tools = f" (tools: {', '.join(entry.tool_names)})"
            lines.append(f"\n[ASSISTANT]{tools} {entry.content[:1000]}")
        elif entry.entry_type == "summary":
            lines.append(f"\n[SUMMARY] {entry.content[:1000]}")

    lines.append("")
    lines.append("=" * 60)
    lines.append("END OF RESTORED CONTEXT")
    lines.append("=" * 60)

    return "\n".join(lines)
