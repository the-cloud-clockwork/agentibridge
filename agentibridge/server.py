#!/usr/bin/env python3
"""AgentiBridge MCP Server.

Indexes and exposes ALL Claude Code CLI transcripts from
~/.claude/projects/ via MCP tools. Background collector polls
for new data; all tools work with Redis or filesystem fallback.

Usage:
    python -m agentibridge

Available tools (17):
    Phase 1 — Foundation:
    - list_sessions       — List sessions across all projects
    - get_session         — Get full session metadata + transcript
    - get_session_segment — Paginated/time-range transcript retrieval
    - get_session_actions — Extract tool calls with counts
    - search_sessions     — Keyword search across transcripts
    - collect_now         — Trigger immediate collection
    Phase 2 — Semantic Search:
    - search_semantic     — Semantic search using embeddings
    - generate_summary    — Auto-generate session summary via LLM
    Phase 4 — Write-back & Dispatch:
    - restore_session     — Load session context for continuation
    - dispatch_task       — Dispatch a task with optional session context
    - get_dispatch_job    — Poll background job status
    - list_dispatch_jobs  — List dispatch jobs with optional status filter
    Phase 5 — Knowledge Catalog:
    - list_memory_files   — List memory files across projects
    - get_memory_file     — Read a specific memory file
    - list_plans          — List plans sorted by recency
    - get_plan            — Read a plan by codename
    - search_history      — Search global prompt history
"""

import json
import os
from pathlib import Path
import sys
from typing import Dict

from mcp.server.fastmcp import FastMCP

from agentibridge.logging import log

_SUMMARY_TRUNCATE_LENGTH = 200


# =============================================================================
# OAUTH SETUP
# =============================================================================


def _build_oauth_config():
    """Build OAuth provider + settings if OAUTH_ISSUER_URL is set."""
    issuer = os.getenv("OAUTH_ISSUER_URL")
    if not issuer:
        return None, None

    from agentibridge.oauth_provider import BridgeOAuthProvider

    try:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    except ImportError:
        print("WARNING: mcp package does not support OAuth (upgrade to >=1.26)", file=sys.stderr)
        return None, None

    client_id = os.getenv("OAUTH_CLIENT_ID", "")
    client_secret = os.getenv("OAUTH_CLIENT_SECRET", "")

    provider = BridgeOAuthProvider(
        issuer_url=issuer,
        client_id=client_id,
        client_secret=client_secret,
    )

    # Always enable registration — claude.ai requires it to work.
    # The provider returns pre-configured creds when locked.
    allowed_scopes_raw = os.getenv("OAUTH_ALLOWED_SCOPES", "").strip()
    allowed_scopes_list = [s.strip() for s in allowed_scopes_raw.split() if s.strip()] if allowed_scopes_raw else None

    resource_url = os.getenv("OAUTH_RESOURCE_URL") or (issuer.rstrip("/") + "/mcp")
    settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=resource_url,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=allowed_scopes_list,
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    return provider, settings


# =============================================================================
# MCP SERVER
# =============================================================================

_oauth_provider, _oauth_settings = _build_oauth_config()

mcp = FastMCP(
    "agentibridge",
    host=os.getenv("AGENTIBRIDGE_HOST", "127.0.0.1"),
    port=int(os.getenv("AGENTIBRIDGE_PORT", "8100")),
    json_response=True,
    auth_server_provider=_oauth_provider,
    auth=_oauth_settings,
)

# Lazy singletons
_store = None
_collector = None
_embedder = None


def _get_store():
    global _store
    if _store is None:
        from agentibridge.store import SessionStore

        _store = SessionStore()
    return _store


def _get_collector():
    global _collector
    if _collector is None:
        from agentibridge.config import AGENTIBRIDGE_ENABLED, AGENTIBRIDGE_EMBEDDING_ENABLED
        from agentibridge.collector import SessionCollector

        embedder = _get_embedder() if AGENTIBRIDGE_EMBEDDING_ENABLED else None
        _collector = SessionCollector(_get_store(), embedder=embedder)
        if AGENTIBRIDGE_ENABLED:
            _collector.start()
    return _collector


def _get_embedder():
    global _embedder
    if _embedder is None:
        from agentibridge.embeddings import TranscriptEmbedder

        _embedder = TranscriptEmbedder()
    return _embedder


# =============================================================================
# MCP TOOLS
# =============================================================================


@mcp.tool()
def list_sessions(
    project: str = "",
    limit: int = 20,
    offset: int = 0,
    since_hours: int = 0,
) -> str:
    """List Claude Code sessions across all projects, sorted by most recent.

    Args:
        project: Filter by project path substring (e.g., "agenticore")
        limit: Maximum sessions to return (default: 20)
        offset: Skip first N results for pagination (default: 0)
        since_hours: Only sessions active in the last N hours (0 = all)

    Returns:
        JSON with sessions list
    """
    try:
        _get_collector()  # ensure collector is running
        store = _get_store()

        sessions = store.list_sessions(
            project=project if project else None,
            limit=limit,
            offset=offset,
            since_hours=since_hours if since_hours > 0 else 0,
        )

        return json.dumps(
            {
                "success": True,
                "count": len(sessions),
                "offset": offset,
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "project_path": s.project_path,
                        "git_branch": s.git_branch,
                        "start_time": s.start_time,
                        "last_update": s.last_update,
                        "num_user_turns": s.num_user_turns,
                        "num_assistant_turns": s.num_assistant_turns,
                        "num_tool_calls": s.num_tool_calls,
                        "summary": s.summary[:_SUMMARY_TRUNCATE_LENGTH],
                        "has_subagents": s.has_subagents,
                    }
                    for s in sessions
                ],
            }
        )

    except Exception as e:
        log("MCP list_sessions failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def get_session(
    session_id: str,
    last_n: int = 50,
    include_meta: bool = True,
) -> str:
    """Get full session details: metadata + conversation transcript.

    Args:
        session_id: Session UUID
        last_n: Number of most recent entries to include (default: 50, 0 = all)
        include_meta: Include session metadata in response (default: True)

    Returns:
        JSON with meta and entries
    """
    try:
        _get_collector()
        store = _get_store()

        result = {"success": True}

        if include_meta:
            meta = store.get_session_meta(session_id)
            if meta:
                result["meta"] = meta.to_dict()
            else:
                return json.dumps({"success": False, "error": f"Session not found: {session_id}"})

        if last_n == 0:
            entries = store.get_session_entries(session_id, offset=0, limit=10000)
        else:
            # Use count_entries to avoid loading all entries just for the count
            total = store.count_entries(session_id)
            start = max(0, total - last_n)
            entries = store.get_session_entries(session_id, offset=start, limit=last_n)

        result["entries"] = [e.to_dict() for e in entries]
        result["entry_count"] = len(entries)

        return json.dumps(result)

    except Exception as e:
        log("MCP get_session failed", {"session_id": session_id, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def get_session_segment(
    session_id: str,
    offset: int = 0,
    limit: int = 20,
    since: str = "",
    until: str = "",
) -> str:
    """Get a segment of session transcript by offset/limit or time range.

    Args:
        session_id: Session UUID
        offset: Start from entry N (0-indexed)
        limit: Number of entries to return (default: 20)
        since: ISO timestamp — only entries after this time
        until: ISO timestamp — only entries before this time

    Returns:
        JSON with entries and total_count
    """
    try:
        _get_collector()
        store = _get_store()

        if since or until:
            # Time-based: get all entries and filter
            all_entries = store.get_session_entries(session_id, offset=0, limit=10000)
            filtered = []
            for e in all_entries:
                if since and e.timestamp < since:
                    continue
                if until and e.timestamp > until:
                    continue
                filtered.append(e)
            entries = filtered[:limit]
            total_count = len(filtered)
        else:
            entries = store.get_session_entries(session_id, offset=offset, limit=limit)
            total_count = store.count_entries(session_id)

        return json.dumps(
            {
                "success": True,
                "session_id": session_id,
                "total_count": total_count,
                "offset": offset,
                "count": len(entries),
                "entries": [e.to_dict() for e in entries],
            }
        )

    except Exception as e:
        log("MCP get_session_segment failed", {"session_id": session_id, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def get_session_actions(
    session_id: str,
    action_types: str = "tool_use",
) -> str:
    """Extract tool calls and file changes from a session.

    Args:
        session_id: Session UUID
        action_types: Types to extract (default: "tool_use")

    Returns:
        JSON with tool call counts and summary
    """
    try:
        _get_collector()
        store = _get_store()

        entries = store.get_session_entries(session_id, offset=0, limit=10000)

        # Count tool usage
        tool_counts: Dict[str, int] = {}
        for entry in entries:
            for tool_name in entry.tool_names:
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1

        # Sort by count descending
        sorted_tools = sorted(tool_counts.items(), key=lambda x: -x[1])

        return json.dumps(
            {
                "success": True,
                "session_id": session_id,
                "total_tool_calls": sum(tool_counts.values()),
                "unique_tools": len(tool_counts),
                "tools": [{"name": name, "count": count} for name, count in sorted_tools],
            }
        )

    except Exception as e:
        log("MCP get_session_actions failed", {"session_id": session_id, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def search_sessions(
    query: str,
    project: str = "",
    limit: int = 10,
) -> str:
    """Keyword search across all session transcripts.

    Args:
        query: Search keyword or phrase
        project: Filter to a specific project (substring match)
        limit: Maximum results (default: 10)

    Returns:
        JSON with matching entries from sessions
    """
    try:
        _get_collector()
        store = _get_store()

        results = store.search_sessions(
            query=query,
            project=project if project else None,
            limit=limit,
        )

        return json.dumps(
            {
                "success": True,
                "query": query,
                "count": len(results),
                "matches": results,
            }
        )

    except Exception as e:
        log("MCP search_sessions failed", {"query": query, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def agent_search(
    query: str,
    model: str = "opus",
    timeout: int = 300,
    extra_instructions: str = "",
) -> str:
    """Reconnaissance search via a headless Claude Code one-shot.

    Wraps the operator's ``query`` in a recon prompt and invokes ``claude -p``
    (bypass permissions, chosen model) to do the legwork — grepping sessions,
    history, files, memory, plans — and return a structured answer.

    Use when a single keyword/semantic search is not enough and you want an
    agent to reason over the results. Cheaper than spinning up an interactive
    Claude Code session just to look something up.

    Args:
        query: The operator's question (the bit that was in quotes).
        model: Model for the one-shot (default: "opus").
        timeout: Max seconds to wait for the CLI (default: 300).
        extra_instructions: Optional extra context appended to the prompt.

    Returns:
        JSON: {success, query, result, session_id, duration_ms, error}.
        ``result`` is the agent's answer (ideally JSON with matches, but
        free-form is tolerated).
    """
    from agentibridge.claude_runner import run_claude_sync

    prompt = (
        "You are a reconnaissance helper for the agentibridge fleet. "
        "The operator asked:\n\n"
        f"  {query}\n\n"
        "Use the MCP tools available to you (list_sessions, search_sessions, "
        "search_history, search_semantic, get_session, list_memory_files, "
        "list_plans, plus Read/Glob/Grep) to find the most relevant sessions, "
        "files, history entries, memory files, or plans that match the query. "
        "Return ONLY a compact JSON object of the form:\n"
        '  {"success": true, "query": "<echo>", "count": N, '
        '"matches": [ {...relevant fields per hit...} ], '
        '"notes": "<one short sentence of context, optional>"}\n'
        "Put the most relevant hits first. No prose outside the JSON."
    )
    if extra_instructions:
        prompt += f"\n\nAdditional instructions:\n{extra_instructions}"

    try:
        result = run_claude_sync(
            prompt,
            model=model,
            timeout=timeout,
            permission_mode="bypassPermissions",
        )
        return json.dumps(
            {
                "success": result.success,
                "query": query,
                "result": result.result,
                "session_id": result.session_id,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
                "error": result.error,
            }
        )
    except Exception as e:
        log("MCP agent_search failed", {"query": query, "error": str(e)})
        return json.dumps({"success": False, "query": query, "error": str(e)})


@mcp.tool()
def collect_now() -> str:
    """Trigger immediate transcript collection.

    Forces the collector to scan all transcript files now instead of
    waiting for the next polling cycle.

    Returns:
        JSON with collection stats
    """
    try:
        collector = _get_collector()
        stats = collector.collect_once()

        return json.dumps(
            {
                "success": True,
                **stats,
            }
        )

    except Exception as e:
        log("MCP collect_now failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# PHASE 2 — SEMANTIC SEARCH
# =============================================================================


@mcp.tool()
def search_semantic(
    query: str,
    project: str = "",
    limit: int = 10,
) -> str:
    """Semantic search across session transcripts using embeddings.

    Requires LLM API configured (LLM_API_BASE + LLM_API_KEY + LLM_EMBED_MODEL) and Postgres (pgvector).
    Sessions must be embedded first via embed_session or auto-embedding.

    Args:
        query: Natural language search query
        project: Filter to a specific project (substring match)
        limit: Maximum results (default: 10)

    Returns:
        JSON with semantically similar session matches ranked by score
    """
    try:
        embedder = _get_embedder()
        if not embedder.is_available():
            return json.dumps(
                {
                    "success": False,
                    "error": "Embedding backend not available. Configure LLM_API_BASE + LLM_API_KEY + LLM_EMBED_MODEL and POSTGRES_URL.",
                }
            )

        results = embedder.search_semantic(
            query=query,
            project=project if project else None,
            limit=limit,
        )

        return json.dumps(
            {
                "success": True,
                "query": query,
                "count": len(results),
                "matches": results,
            }
        )

    except Exception as e:
        log("MCP search_semantic failed", {"query": query, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def generate_summary(
    session_id: str,
) -> str:
    """Generate an AI summary for a session using Claude.

    Reads the session transcript and produces a 2-3 sentence summary
    of what was accomplished, key decisions, and outcomes.

    Args:
        session_id: Session UUID to summarize

    Returns:
        JSON with the generated summary text
    """
    try:
        embedder = _get_embedder()
        summary = embedder.generate_summary(session_id)

        return json.dumps(
            {
                "success": True,
                "session_id": session_id,
                "summary": summary,
            }
        )

    except Exception as e:
        log("MCP generate_summary failed", {"session_id": session_id, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# PHASE 4 — WRITE-BACK & DISPATCH
# =============================================================================


@mcp.tool()
def restore_session(
    session_id: str,
    last_n: int = 20,
) -> str:
    """Load session context blob for injection into a new conversation.

    Extracts the most relevant context from a past session, formatted
    for use as context in a new agent call or conversation.

    Args:
        session_id: Session UUID to restore context from
        last_n: Number of recent turns to include (default: 20)

    Returns:
        JSON with formatted context string ready for injection
    """
    try:
        from agentibridge.dispatch import restore_session_context

        context = restore_session_context(session_id, last_n=last_n)

        return json.dumps(
            {
                "success": True,
                "session_id": session_id,
                "context": context,
                "char_count": len(context),
            }
        )

    except Exception as e:
        log("MCP restore_session failed", {"session_id": session_id, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def dispatch_task(
    task_description: str,
    project: str = "",
    session_id: str = "",
    resume_session_id: str = "",
    command: str = "default",
    context_turns: int = 10,
) -> str:
    """Dispatch a task to the agent as a background job (fire-and-forget).

    Returns immediately with a job_id. Use get_dispatch_job(job_id) to
    check status and retrieve output when the task completes.

    Two ways to use a past session:
    - session_id: load context from the session and inject it into a new prompt
    - resume_session_id: actually resume the session thread via ``--resume``
      (continues the existing conversation with full memory, no injection needed)

    Args:
        task_description: What the agent should do
        project: Project context hint (optional). Can be a full path or fuzzy name (e.g. "agentibridge") — resolved to cwd for the spawned session
        session_id: Past session to pull context from (optional)
        resume_session_id: Session to resume via --resume flag (optional)
        command: Command preset — default/thinkhard/ultrathink
        context_turns: Number of turns to include from session context

    Returns:
        JSON with job_id and status "running"
    """
    try:
        from agentibridge.dispatch import dispatch_task as _dispatch

        result = await _dispatch(
            task_description=task_description,
            project=project,
            session_id=session_id,
            resume_session_id=resume_session_id,
            command=command,
            context_turns=context_turns,
        )

        return json.dumps({"success": True, **result})

    except Exception as e:
        log("MCP dispatch_task failed", {"task": task_description, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def get_dispatch_job(job_id: str) -> str:
    """Get the status and output of a background dispatch job.

    Args:
        job_id: Job UUID returned by dispatch_task

    Returns:
        JSON with status ("running", "completed", "failed"), output, error,
        duration_ms, claude_session_id, and other metadata.
    """
    from agentibridge.dispatch import get_job_status

    data = get_job_status(job_id)
    if data is None:
        return json.dumps({"success": False, "error": f"Job not found: {job_id}"})
    return json.dumps({"success": True, **data})


@mcp.tool()
async def list_dispatch_jobs(status: str = "", limit: int = 20) -> str:
    """List dispatch jobs with optional status filter.

    Returns job summaries (newest first) without the full output field,
    so the response stays compact even with many jobs.

    Args:
        status: Filter by status ("running", "completed", "failed"). Empty = all.
        limit: Maximum number of jobs to return (default: 20)

    Returns:
        JSON with jobs list and count
    """
    try:
        from agentibridge.dispatch import list_jobs

        jobs = list_jobs(status=status, limit=limit)
        return json.dumps({"success": True, "count": len(jobs), "jobs": jobs})

    except Exception as e:
        log("MCP list_dispatch_jobs failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# PHASE 4b — DISPATCH PLANS (plan-then-execute workflow)
# =============================================================================


@mcp.tool()
async def plan_task(
    task: str,
    repo_url: str = "",
    wait: bool = False,
    timeout: int = 0,
) -> str:
    """Create an implementation plan without executing it.

    Runs Claude in read-only mode (Read, Glob, Grep only) to analyse the
    codebase and produce a markdown plan. The plan can later be executed
    with execute_plan.

    Args:
        task: What to plan (same format as dispatch_task)
        repo_url: Repo to analyse (optional)
        wait: Block until plan is ready (default: false)
        timeout: Timeout in seconds (0 = use default from env)

    Returns:
        JSON with plan_id, job_id, status, and (if wait=true) the plan content
    """
    try:
        from agentibridge.plans import submit_plan

        result = await submit_plan(task=task, repo_url=repo_url, wait=wait, timeout=timeout)
        return json.dumps({"success": True, **result})

    except Exception as e:
        log("MCP plan_task failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def get_dispatch_plan(plan_id: str) -> str:
    """Get a plan by ID, including its markdown content once ready.

    Args:
        plan_id: Plan UUID returned by plan_task

    Returns:
        JSON with plan details including status and content
    """
    try:
        from agentibridge.plans import get_plan_status

        data = get_plan_status(plan_id)
        if data is None:
            return json.dumps({"success": False, "error": f"Plan not found: {plan_id}"})
        return json.dumps({"success": True, **data})

    except Exception as e:
        log("MCP get_dispatch_plan failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def list_dispatch_plans(status: str = "", limit: int = 20) -> str:
    """List dispatch plans with optional status filter.

    Returns plan summaries (newest first) without the content field,
    so the response stays compact even with many plans.

    Args:
        status: Filter by status (planning/ready/failed/executing/completed). Empty = all.
        limit: Maximum number of plans to return (default: 20)

    Returns:
        JSON with plans list and count
    """
    try:
        from agentibridge.plans import list_plans as list_dispatch_plans_fn

        plans = list_dispatch_plans_fn(status=status, limit=limit)
        return json.dumps({"success": True, "count": len(plans), "plans": plans})

    except Exception as e:
        log("MCP list_dispatch_plans failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def execute_plan(
    plan_id: str,
    repo_url: str = "",
    wait: bool = False,
    timeout: int = 0,
) -> str:
    """Execute a ready plan by ID.

    Submits a normal coding job with the plan injected as context.

    Args:
        plan_id: Plan ID returned by plan_task
        repo_url: Override repo URL (defaults to the one used when planning)
        wait: Block until execution completes
        timeout: Timeout in seconds (0 = use default from env)

    Returns:
        JSON with job_id and status
    """
    try:
        from agentibridge.plans import execute_plan as execute_plan_fn

        result = await execute_plan_fn(plan_id=plan_id, repo_url=repo_url, wait=wait, timeout=timeout)
        return json.dumps({"success": True, **result})

    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)})

    except Exception as e:
        log("MCP execute_plan failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# HANDOFF — Cross-project context transfer
# =============================================================================


@mcp.tool()
def list_handoff_projects() -> str:
    """List available projects for handoff.

    Scans ~/.claude/projects/ and returns decoded project paths with
    session counts. Use this to discover valid targets for the handoff tool.

    Returns:
        JSON with projects list
    """
    try:
        from agentibridge.catalog import list_projects
        from agentibridge.config import CLAUDE_CODE_HOME_DIR

        base_dir = Path(CLAUDE_CODE_HOME_DIR) / "projects"
        projects = list_projects(base_dir)

        return json.dumps(
            {
                "success": True,
                "count": len(projects),
                "projects": [p.to_dict() for p in projects],
            }
        )

    except Exception as e:
        log("MCP list_handoff_projects failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def handoff(
    project_path: str,
    summary: str,
    decisions: str,
    next_steps: str,
    context: str = "",
    source_session_id: str = "",
    model: str = "sonnet",
) -> str:
    """Hand off context to a target project as a new conversation.

    Creates a seeded Claude session in the target project with structured
    context (summary, decisions, next steps). Blocks until the session is
    created. Returns the session_id so the operator can resume with:
        claude --resume <session_id>

    The project_path can be a full path, an encoded project name, or a
    fuzzy name like "agenticore" — it will be resolved against known projects.

    Args:
        project_path: Target project path or fuzzy name (e.g., "agenticore")
        summary: What was accomplished in the current session
        decisions: Key decisions made
        next_steps: What the target session should do
        context: Optional freeform additional context
        source_session_id: Optional session to pull extra context from
        model: Model for the target session (default: sonnet)

    Returns:
        JSON with session_id, project_path, output, duration_ms
    """
    try:
        from agentibridge.catalog import resolve_project
        from agentibridge.config import CLAUDE_CODE_HOME_DIR
        from agentibridge.dispatch import handoff as _handoff

        # Resolve fuzzy project name to full path
        resolved_path = project_path
        if not Path(project_path).is_absolute():
            base_dir = Path(CLAUDE_CODE_HOME_DIR) / "projects"
            match = resolve_project(base_dir, project_path)
            if match is None:
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Project not found: {project_path}",
                    }
                )
            resolved_path = match.path

        result = await _handoff(
            project_path=resolved_path,
            summary=summary,
            decisions=decisions,
            next_steps=next_steps,
            context=context,
            source_session_id=source_session_id,
            model=model,
        )

        return json.dumps(result)

    except Exception as e:
        log("MCP handoff failed", {"project": project_path, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# PHASE 5 — KNOWLEDGE CATALOG (Memory, Plans, History)
# =============================================================================


@mcp.tool()
def list_memory_files(project: str = "") -> str:
    """List all memory files across projects.

    Memory files (~/.claude/projects/{project}/memory/*.md) contain curated
    project knowledge — the highest-signal content per project.

    Args:
        project: Filter by project path substring (e.g., "agentibridge")

    Returns:
        JSON with files list
    """
    try:
        _get_collector()
        store = _get_store()

        files = store.list_memory_files(project=project if project else None)

        return json.dumps(
            {
                "success": True,
                "count": len(files),
                "files": [
                    {
                        "project_path": f.project_path,
                        "project_encoded": f.project_encoded,
                        "filename": f.filename,
                        "file_size_bytes": f.file_size_bytes,
                        "last_modified": f.last_modified,
                    }
                    for f in files
                ],
            }
        )

    except Exception as e:
        log("MCP list_memory_files failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def get_memory_file(project: str, filename: str = "MEMORY.md") -> str:
    """Read a specific memory file's content.

    Args:
        project: Project encoded name (e.g., "-home-user-dev-myapp")
        filename: Memory filename (default: "MEMORY.md")

    Returns:
        JSON with project_path, filename, content, file_size_bytes, last_modified
    """
    try:
        _get_collector()
        store = _get_store()

        mem = store.get_memory_file(project, filename)
        if mem is None:
            return json.dumps({"success": False, "error": f"Memory file not found: {project}/{filename}"})

        return json.dumps(
            {
                "success": True,
                "project_path": mem.project_path,
                "project_encoded": mem.project_encoded,
                "filename": mem.filename,
                "content": mem.content,
                "file_size_bytes": mem.file_size_bytes,
                "last_modified": mem.last_modified,
            }
        )

    except Exception as e:
        log("MCP get_memory_file failed", {"project": project, "filename": filename, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def list_plans(
    project: str = "",
    codename: str = "",
    limit: int = 30,
    offset: int = 0,
    include_agent_plans: bool = False,
) -> str:
    """List plans sorted by recency.

    Plans (~/.claude/plans/*.md) are detailed implementation blueprints
    linked to sessions via the codename/slug field.

    Args:
        project: Filter by project path substring
        codename: Filter by codename substring
        limit: Maximum plans to return (default: 30)
        offset: Skip first N results for pagination
        include_agent_plans: Include agent subplans (default: False)

    Returns:
        JSON with plans list
    """
    try:
        _get_collector()
        store = _get_store()

        plans = store.list_plans(
            project=project if project else None,
            codename=codename if codename else None,
            limit=limit,
            offset=offset,
            include_agent_plans=include_agent_plans,
        )

        return json.dumps(
            {
                "success": True,
                "count": len(plans),
                "offset": offset,
                "plans": [
                    {
                        "codename": p.codename,
                        "file_size_bytes": p.file_size_bytes,
                        "last_modified": p.last_modified,
                        "is_agent_plan": p.is_agent_plan,
                        "parent_codename": p.parent_codename,
                        "session_ids": p.session_ids,
                        "project_path": p.project_path,
                    }
                    for p in plans
                ],
            }
        )

    except Exception as e:
        log("MCP list_plans failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def get_plan(codename: str, include_agent_plans: bool = False) -> str:
    """Read a plan by codename.

    Args:
        codename: Plan codename (e.g., "cached-wondering-sloth")
        include_agent_plans: Include agent subplans (default: False)

    Returns:
        JSON with codename, content, session_ids, project_path, agent_plans
    """
    try:
        _get_collector()
        store = _get_store()

        result = store.get_plan(codename, include_agent_plans=include_agent_plans)
        if result is None:
            return json.dumps({"success": False, "error": f"Plan not found: {codename}"})

        plan = result["plan"]
        response = {
            "success": True,
            "codename": plan.codename,
            "content": plan.content,
            "file_size_bytes": plan.file_size_bytes,
            "last_modified": plan.last_modified,
            "session_ids": plan.session_ids,
            "project_path": plan.project_path,
        }

        if include_agent_plans:
            response["agent_plans"] = [
                {
                    "codename": ap.codename,
                    "content": ap.content,
                    "file_size_bytes": ap.file_size_bytes,
                    "last_modified": ap.last_modified,
                }
                for ap in result["agent_plans"]
            ]
        else:
            response["agent_plans"] = []

        return json.dumps(response)

    except Exception as e:
        log("MCP get_plan failed", {"codename": codename, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def search_history(
    query: str = "",
    project: str = "",
    session_id: str = "",
    limit: int = 20,
    offset: int = 0,
    since: str = "",
) -> str:
    """Search the global prompt history.

    History (~/.claude/history.jsonl) contains every user prompt across all
    sessions with timestamps, project paths, and session UUIDs.

    Args:
        query: Search keyword or phrase (empty = all)
        project: Filter by project path substring
        session_id: Filter by session UUID
        limit: Maximum results (default: 20)
        offset: Skip first N results for pagination
        since: ISO timestamp — only entries after this time

    Returns:
        JSON with entries list and total count
    """
    try:
        _get_collector()
        store = _get_store()

        entries, total = store.search_history(
            query=query,
            project=project if project else None,
            session_id=session_id if session_id else None,
            limit=limit,
            offset=offset,
            since=since,
        )

        return json.dumps(
            {
                "success": True,
                "total": total,
                "count": len(entries),
                "offset": offset,
                "entries": [e.to_dict() for e in entries],
            }
        )

    except Exception as e:
        log("MCP search_history failed", {"query": query, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# PHASE 6 — AGENT REGISTRY (A2A Discovery)
# =============================================================================


@mcp.tool()
def register_agent(
    agent_id: str,
    agent_name: str = "",
    agent_type: str = "",
    capabilities: str = "[]",
    endpoint: str = "",
    metadata: str = "{}",
    heartbeat_ttl: int = 300,
    transport: str = "http",
) -> str:
    """Register an agent for A2A discovery.

    Agents call this on boot to announce themselves. Idempotent — safe to
    call repeatedly (upsert). Capabilities and metadata are JSON strings.

    Args:
        agent_id: Unique agent identifier (e.g., "agenticore-dev-0")
        agent_name: Human-readable name
        agent_type: Category (e.g., "executor", "observer", "specialist")
        capabilities: JSON array of capability strings
        endpoint: How to reach this agent (URL). Empty for local transport.
        metadata: JSON object with arbitrary key-value pairs. For transport
            "local", include "package_path" so tasks can be dispatched by
            spawning claude in that directory.
        heartbeat_ttl: Seconds before agent is considered offline (default: 300)
        transport: Delivery transport — "http" (POST endpoint/jobs, default) or
            "local" (spawn a fresh claude in metadata.package_path)

    Returns:
        JSON with registration result
    """
    try:
        from agentibridge.registry import register_agent as _register

        caps = json.loads(capabilities) if capabilities else []
        meta = json.loads(metadata) if metadata else {}
        result = _register(
            agent_id=agent_id,
            agent_name=agent_name,
            agent_type=agent_type,
            capabilities=caps,
            endpoint=endpoint,
            metadata=meta,
            heartbeat_ttl=heartbeat_ttl,
            transport=transport,
        )
        return json.dumps({"success": True, **result})
    except Exception as e:
        log("MCP register_agent failed", {"agent_id": agent_id, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def deregister_agent(agent_id: str) -> str:
    """Remove an agent from the registry.

    Args:
        agent_id: Agent identifier to remove

    Returns:
        JSON with deletion result
    """
    try:
        from agentibridge.registry import deregister_agent as _deregister

        result = _deregister(agent_id)
        return json.dumps({"success": True, **result})
    except Exception as e:
        log("MCP deregister_agent failed", {"agent_id": agent_id, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def heartbeat_agent(
    agent_id: str,
    status: str = "online",
    metadata: str = "{}",
) -> str:
    """Update agent heartbeat timestamp and status.

    Agents call this periodically to signal liveness. If no heartbeat
    is received within heartbeat_ttl, the agent is reported as offline.

    Args:
        agent_id: Agent identifier
        status: Current status ("online", "degraded")
        metadata: JSON object merged into existing metadata

    Returns:
        JSON with heartbeat result
    """
    try:
        from agentibridge.registry import heartbeat_agent as _heartbeat

        meta = json.loads(metadata) if metadata else {}
        result = _heartbeat(agent_id=agent_id, status=status, metadata=meta)
        return json.dumps({"success": True, **result})
    except Exception as e:
        log("MCP heartbeat_agent failed", {"agent_id": agent_id, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def list_agents(
    agent_type: str = "",
    capability: str = "",
    status: str = "",
    limit: int = 50,
) -> str:
    """List registered agents with optional filters.

    Args:
        agent_type: Filter by agent type (e.g., "executor")
        capability: Filter by capability (e.g., "profile:coding")
        status: Filter by effective status ("online", "offline", "degraded")
        limit: Maximum agents to return (default: 50)

    Returns:
        JSON with agents list and count
    """
    try:
        from agentibridge.registry import list_agents as _list

        agents = _list(
            agent_type=agent_type,
            capability=capability,
            status=status,
            limit=limit,
        )
        return json.dumps({"success": True, "count": len(agents), "agents": agents})
    except Exception as e:
        log("MCP list_agents failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def get_agent(agent_id: str) -> str:
    """Get full details of a registered agent.

    Args:
        agent_id: Agent identifier

    Returns:
        JSON with agent record including effective_status
    """
    try:
        from agentibridge.registry import get_agent as _get

        agent = _get(agent_id)
        if agent is None:
            return json.dumps({"success": False, "error": f"Agent not found: {agent_id}"})
        return json.dumps({"success": True, "agent": agent})
    except Exception as e:
        log("MCP get_agent failed", {"agent_id": agent_id, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def find_agents(capability: str) -> str:
    """Find agents that have a specific capability.

    Args:
        capability: Capability string (e.g., "run_task", "profile:coding", "agent_mode")

    Returns:
        JSON with matching agents
    """
    try:
        from agentibridge.registry import find_agents as _find

        agents = _find(capability)
        return json.dumps(
            {
                "success": True,
                "capability": capability,
                "count": len(agents),
                "agents": agents,
            }
        )
    except Exception as e:
        log("MCP find_agents failed", {"capability": capability, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def discover_local_agents(status: str = "") -> str:
    """Discover session-gated local agents (AgentiHub packages) on this host.

    Local agents are Claude Code packages under
    ``<AGENTIHUB_DIR>/agents/<name>/package/``, discovered by scanning the
    filesystem (never persisted). Requires AGENTIBRIDGE_LOCAL_AGENTS_ENABLED=true.

    EVERY local agent returned here is callable via run_agent / dispatch_to_agent,
    regardless of status. Status conveys WARMTH, not reachability:
      - "online" — a claude session is live in its package dir right now.
      - "idle"   — no live session; dispatch cold-starts a fresh claude.
    "idle" is NOT "offline" and NOT an error. Never warn the user that a call
    "may fail to reach" an idle agent — it will succeed.

    Args:
        status: Optional filter by status ("online", "idle")

    Returns:
        JSON with the resolved agentihub path, whether the feature is enabled,
        and the list of local agents with effective_status
    """
    try:
        from agentibridge.local_agents import (
            discover_local_agents as _discover,
            filter_records,
            local_agents_enabled,
            resolve_agentihub_dir,
        )
        from agentibridge.registry import _list_registered

        hub = resolve_agentihub_dir()
        agents = filter_records(_discover(), status=status)
        # A registered record of the same id wins in get_agent/route_to_agent, so
        # mark shadowed entries — otherwise this tool would advertise a local
        # dispatch that would actually be routed to the registered record.
        registered_ids = {a.get("agent_id") for a in _list_registered(limit=1000)}
        for a in agents:
            a["metadata"]["shadowed_by_registered"] = a["agent_id"] in registered_ids
        return json.dumps(
            {
                "success": True,
                "enabled": local_agents_enabled(),
                "agentihub": str(hub) if hub else None,
                "count": len(agents),
                "agents": agents,
            }
        )
    except Exception as e:
        log("MCP discover_local_agents failed", {"error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def run_agent(
    agent_id: str,
    task: str,
    profile: str = "",
    repo_url: str = "",
    wait: bool = False,
    file_path: str = "",
) -> str:
    """Route a task to a specific registered agent.

    LOCAL AGENTS (transport="local", agent_type="local") ARE ALWAYS CALLABLE —
    including when their status is "idle". "idle" means only that no claude
    session is currently running in the agent's package dir; dispatch cold-starts
    a fresh one. Do NOT hedge, warn, or skip the call because an agent is idle.
    Only HTTP agents require "online" (an offline pod genuinely cannot serve).

    For HTTP agents: looks up the agent, checks it's online and has capacity, then
    forwards the task to its REST API. Returns job_id from the target agent.

    Args:
        agent_id: Target agent identifier (e.g., "finops", "agenticore-0")
        task: What the agent should do
        profile: Execution profile (optional — agent uses its default if omitted)
        repo_url: GitHub repo URL (optional)
        wait: If true, block until job completes (default: false)
        file_path: Path to .mcp.json on shared FS (optional)

    Returns:
        JSON with success, agent_id, job details, or error with retry flag
    """
    try:
        from agentibridge.registry import route_to_agent

        result = await route_to_agent(
            agent_id=agent_id,
            task=task,
            profile=profile,
            repo_url=repo_url,
            wait=wait,
            file_path=file_path,
        )
        return json.dumps(result)
    except Exception as e:
        log("MCP run_agent failed", {"agent_id": agent_id, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def dispatch_to_agent(
    capability: str,
    task: str,
    profile: str = "",
    repo_url: str = "",
    wait: bool = False,
    file_path: str = "",
) -> str:
    """Route a task to the best available agent with a specific capability.

    Local agents are candidates even when "idle" — dispatch cold-starts a fresh
    claude in their package dir, so an idle local agent is fully callable. A warm
    (online) agent is preferred when several share the capability. Only HTTP
    agents must be online to be selected.

    Args:
        capability: Required capability (e.g., "profile:coding", "agent:publishing", "agent_mode")
        task: What the agent should do
        profile: Execution profile (optional)
        repo_url: GitHub repo URL (optional)
        wait: If true, block until job completes (default: false)
        file_path: Path to .mcp.json on shared FS (optional)

    Returns:
        JSON with success, agent_id, routed_by, job details, or error with retry flag
    """
    try:
        from agentibridge.registry import route_by_capability

        result = await route_by_capability(
            capability=capability,
            task=task,
            profile=profile,
            repo_url=repo_url,
            wait=wait,
            file_path=file_path,
        )
        return json.dumps(result)
    except Exception as e:
        log("MCP dispatch_to_agent failed", {"capability": capability, "error": str(e)})
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# MAIN
# =============================================================================


def main():
    """Run the AgentiBridge MCP server."""
    from agentibridge.config import AGENTIBRIDGE_REMOVE_TOOLS

    print("Starting AgentiBridge MCP server...", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    for name in AGENTIBRIDGE_REMOVE_TOOLS:
        try:
            mcp._tool_manager.remove_tool(name)
            print(f"  Removed tool: {name}", file=sys.stderr)
        except Exception:
            print(f"  Warning: tool '{name}' not found, skipping", file=sys.stderr)

    available_tools = mcp._tool_manager.list_tools()
    print(f"Available tools: {len(available_tools)}", file=sys.stderr)
    for tool in available_tools:
        print(f"  - {tool.name}", file=sys.stderr)

    print("=" * 60, file=sys.stderr)

    # Start collector eagerly so indexing + embedding begin immediately
    _get_collector()

    transport = os.getenv("AGENTIBRIDGE_TRANSPORT", "stdio")
    if transport == "sse":
        from agentibridge.transport import run_sse_server

        print(f"Starting SSE transport on {mcp.settings.host}:{mcp.settings.port}...", file=sys.stderr)
        run_sse_server(mcp)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
