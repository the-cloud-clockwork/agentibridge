---
title: Internals
nav_order: 5
parent: Architecture
---

# Internal Architecture

This document provides a deep dive into AgentiBridge's internal modules and implementation patterns.

## Key Modules

| Module | Purpose | Key Functions/Classes |
|--------|---------|----------------------|
| `server.py` | FastMCP server with 33 tools | tool handlers, `main()` |
| `parser.py` | Pure-function JSONL transcript parser | `parse_transcript_entries()`, `scan_projects_dir()` |
| `store.py` | SessionStore (Redis + filesystem fallback) | `SessionStore`, `get_session_meta()`, `list_sessions()` |
| `collector.py` | Background polling daemon | `SessionCollector`, `collect_once()` |
| `transport.py` | SSE/HTTP transport + API key auth + OAuth | `run_sse_server()`, auth middleware |
| `oauth_provider.py` | OAuth 2.1 authorization server (opt-in) | `BridgeOAuthProvider` |
| `embeddings.py` | Semantic search (Phase 2) | `TranscriptEmbedder`, `search_semantic()` |
| `dispatch.py` | Session restore + task dispatch (Phase 4) | `restore_session_context()`, `dispatch_task()` |
| `dispatch.py` | Host-side HTTP bridge for Docker dispatch | `GET /health`, `POST /dispatch` |
| `claude_runner.py` | Claude CLI subprocess wrapper | `run_claude()`, `ClaudeResult` |
| `llm_client.py` | OpenAI-compatible embeddings + chat | `embed_text()`, `chat_completion()` |
| `redis_client.py` | Redis helper | `get_redis()`, `redis_key()` |
| `pg_client.py` | Postgres + pgvector connection | `get_pg()`, auto-schema creation |
| `config.py` | Centralized env-var configuration | module-level constants |
| `cli.py` | CLI helper tool | `run`, `stop`, `status`, `tunnel`, `bridge`, `locks` |
| `catalog.py` | Knowledge catalog: memory, plans, history | `scan_memory_files()`, `scan_plans_dir()`, `parse_history()` |
| `logging.py` | Structured JSON logging | `log()` |
| `registry.py` | A2A agent registry: register/heartbeat/route (Phase 6) | `AgentRecord`, `register_agent()`, `list_agents()`, `route_to_agent()`, `route_by_capability()` |
| `local_agents.py` | Session-gated local agent discovery (Phase 6) | `discover_local_agents()`, `get_local_agent()`, `read_package_manifest()` |
| `plans.py` | Plan-then-execute dispatch workflow | `submit_plan()`, `execute_plan()`, `list_plans()`, `get_plan_status()` |
| `claude_assets.py` | Installs bundled skills/commands/agents/rules into `~/.claude/` | `install_claude_assets()`, `uninstall_claude_assets()` |

## Redis + File Fallback Pattern

All stateful operations follow a consistent fallback pattern to ensure reliability even if Redis is unavailable:

### Pattern Overview

```python
# 1. Try Redis first
redis_client = get_redis()
if redis_client:
    try:
        result = redis_client.get(f"{KEY_PREFIX}:sb:{key}")
        if result:
            return json.loads(result)
    except Exception as e:
        logger.warning(f"Redis error, falling back to filesystem: {e}")

# 2. Fall back to filesystem
return read_from_jsonl_file(path)
```

### Key Characteristics

- **Graceful degradation**: If Redis is down, the bridge continues working with direct file reads
- **No partial failures**: Either operation succeeds completely or falls back
- **Namespaced keys**: All Redis keys use `{REDIS_KEY_PREFIX}:sb:{suffix}` format
- **Idempotent operations**: Safe to retry without side effects

### Redis Key Schema

```
agentibridge:sb:idx:all                  # Sorted set of all session IDs (score = last_update)
agentibridge:sb:idx:project:{encoded}    # Sorted set of session IDs per project
agentibridge:sb:session:{id}:meta        # Hash of session metadata fields
agentibridge:sb:session:{id}:entries     # List of JSON-serialized entries (capped at MAX_ENTRIES)
agentibridge:sb:pos:{filepath_hash}      # Byte offset for incremental transcript reading

# Phase 5 — Knowledge Catalog
agentibridge:sb:memory:{project}:{filename}        # Hash of memory file metadata + content
agentibridge:sb:idx:memory                         # Sorted set of all memory file keys (score = mtime)
agentibridge:sb:plan:{codename}                    # Hash of plan metadata + content
agentibridge:sb:plan:{codename}:agents             # List of agent subplan codenames
agentibridge:sb:idx:plans                          # Sorted set of all plan codenames (score = mtime)
agentibridge:sb:codename:{slug}                    # Set of session IDs linked to a plan codename
agentibridge:sb:history                            # List of JSON-serialized history entries
agentibridge:sb:pos:history                        # Byte offset for incremental history.jsonl parsing
```

### When Redis is Used

- **list_sessions**: Fast ID enumeration (`SMEMBERS sessions`)
- **get_session**: Quick metadata lookup (`HGETALL session:{id}`)
- **Collector**: Locks prevent concurrent processing of same project
- **Transcript caching**: Avoids re-parsing large JSONL files on every request

### When Filesystem is Used

- **Redis unavailable**: All operations fall back to direct file reads
- **Segment queries**: Time-range filters read directly from JSONL (no caching benefit)
- **Full transcript**: If `MAX_ENTRIES=0` or not in Redis, reads from file

## Transcript Format

### File Location

Raw transcripts are stored in: `~/.claude/projects/{path-encoded}/{session-id}.jsonl`

**Path encoding example:**
- Project path: `/home/user/dev/myproject`
- Encoded name: `-home-user-dev-myproject`
- Full path: `~/.claude/projects/-home-user-dev-myproject/`

### Entry Types

Each line in the JSONL file is a JSON object with a `type` field:

**Indexed types:**
- `user` — User input (prompts, commands)
- `assistant` — Assistant responses (text, tool calls)
- `summary` — Session summary metadata
- `system` — System messages (hooks, errors)

**Filtered types (not indexed):**
- `queue-operation` — Internal task queue events
- `file-history-snapshot` — File state snapshots
- `progress` — Progress indicators

### Entry Structure

```json
{
  "type": "assistant",
  "timestamp": "2026-02-20T12:34:56.789Z",
  "content": "Let me help you with that...",
  "tool_calls": [
    {
      "name": "Read",
      "parameters": {"file_path": "/path/to/file.py"}
    }
  ]
}
```

### Parsing Logic

The `parser.py` module provides pure functions for incremental parsing:

```python
# Scan all projects under ~/.claude/projects/
sessions = scan_projects_dir(projects_dir)

# Parse new entries starting from a byte offset (incremental)
entries, new_offset = parse_transcript_entries(transcript_path, offset=last_offset)

# Extract session metadata (git branch, cwd, counts, etc.)
meta = parse_transcript_meta(transcript_path)
```

Indexed entry types: `user`, `assistant`, `summary`, `system`.
Skipped types: `progress`, `queue-operation`, `file-history-snapshot`.

## Collector Daemon

### Polling Loop

```
┌─────────────────────────────┐
│ Every POLL_INTERVAL seconds │
└──────────┬──────────────────┘
           │
           ▼
    ┌──────────────┐
    │ Scan projects│
    │  directory   │
    └──────┬───────┘
           │
           ▼
    ┌──────────────────┐
    │ For each project:│
    │  - Acquire lock  │
    │  - Find new data │
    │  - Parse & index │
    │  - Release lock  │
    └──────────────────┘
```

### Lock Mechanism

Uses Redis locks to prevent concurrent indexing:

```python
lock_key = f"{KEY_PREFIX}:sb:lock:collect:{project_hash}"
if redis.set(lock_key, "1", nx=True, ex=300):  # 5-minute lock
    try:
        collect_project(project_path)
    finally:
        redis.delete(lock_key)
```

Without Redis, collection skips the lock and proceeds directly (no concurrent protection).

### Incremental Updates

Tracks last-processed byte offset per transcript file:

```python
position_key = f"{KEY_PREFIX}:sb:pos:{hash(filepath)}"
last_offset = int(redis.get(position_key) or 0)
entries, new_offset = parse_transcript_entries(filepath, offset=last_offset)
redis.set(position_key, new_offset)
```

Without Redis, positions are stored under `~/.cache/agentibridge/positions/`.

## Transport Layer (Phase 3)

### stdio Transport

For local MCP clients (Claude Code CLI):

```
# Reads from stdin, writes to stdout
# Used when AGENTIBRIDGE_TRANSPORT=stdio
stdin -> MCP request -> process -> MCP response -> stdout
```

### HTTP/SSE Transport

For remote MCP clients (ChatGPT, Claude Web, etc.):

```
GET  /health                             -> {"status": "ok"}  (public)
POST /mcp                                -> Streamable HTTP (preferred)
GET  /sse                                -> Server-Sent Events (legacy)
GET  /.well-known/oauth-authorization-server -> OAuth metadata (if OAuth enabled)
POST /token, /authorize, /register, /revoke  -> OAuth 2.1 endpoints (if OAuth enabled)
```

**Authentication options:**
- API key: `X-API-Key: your-key` header or `?api_key=your-key` query param
- OAuth 2.1: Bearer token via `Authorization: Bearer <token>` (enabled by `OAUTH_ISSUER_URL`)

## Embedding Pipeline (Phase 2)

### Vector Storage

```
1. Transcript entry (text)
   ↓
2. LLM API (OpenAI-compatible)
   ↓ embed()
3. Vector (e.g., 1536 dimensions)
   ↓
4. PostgreSQL + pgvector
   ↓ similarity search
5. Ranked results
```

### Schema

```sql
CREATE TABLE IF NOT EXISTS transcript_chunks (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    chunk_idx       INTEGER NOT NULL,
    project         TEXT NOT NULL DEFAULT '',
    project_encoded TEXT NOT NULL DEFAULT '',
    timestamp       TEXT NOT NULL DEFAULT '',
    text_preview    TEXT NOT NULL DEFAULT '',
    embedding       vector(1536),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, chunk_idx)
);

CREATE INDEX idx_tc_embedding_hnsw ON transcript_chunks
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

### Search Query

```python
def search_semantic(query: str, limit: int = 10) -> list[dict]:
    query_vector = llm_client.embed_text(query)
    results = pg.execute("""
        SELECT session_id, text_preview,
               1 - (embedding <=> %s::vector) AS similarity
        FROM transcript_chunks
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """, (query_vector, query_vector, limit))
    return results
```

## Dispatch Architecture (Phase 4)

### Session Restore

`restore_session_context(session_id, last_n)` loads recent turns from a past session and formats them as a text block for injection into a new prompt. Returns a `dict` with the formatted `context` string and `char_count`.

### Task Dispatch

`dispatch_task(...)` is fully async and fire-and-forget:

1. Writes job state to `/tmp/agentibridge_jobs/{job_id}.json` immediately
2. Starts an `asyncio.create_task()` — returns `job_id` to the caller
3. Background task calls `run_claude()` (local subprocess or HTTP bridge)
4. On completion, updates the job file with output, exit_code, duration_ms

Clients poll with `get_dispatch_job(job_id)` until `status` is `completed` or `failed`.

**Dispatch modes:**
- **Local**: `CLAUDE_DISPATCH_URL` is empty → runs `claude` subprocess directly
- **Bridge**: `CLAUDE_DISPATCH_URL` is set → HTTP POST to `dispatch.py` on the host

## Knowledge Catalog (Phase 5)

### Data Sources

Phase 5 exposes three knowledge categories from Claude Code's local filesystem:

| Source | Location | Format |
|--------|----------|--------|
| **Memory files** | `~/.claude/projects/{project}/memory/*.md` | Markdown files with curated project knowledge |
| **Plans** | `~/.claude/plans/*.md` | Implementation blueprints linked to sessions via codename/slug |
| **History** | `~/.claude/history.jsonl` | Every user prompt across all sessions with timestamps |

### Agent Plans

Plans with the suffix `-agent-{hex_hash}` are subplans created by agent subprocesses. They're linked to their parent plan by stripping the suffix:

- `moonlit-rolling-reddy.md` — parent plan
- `moonlit-rolling-reddy-agent-a1b2c3.md` — agent subplan

When `include_agent_plans=True`, `get_plan` returns the parent content plus all linked agent subplans.

### Incremental History Parsing

The `parse_history()` function uses byte-offset tracking to avoid re-reading the entire `history.jsonl` on every collection cycle:

1. Seek to the last known byte offset
2. Detect if offset is at a line boundary (peek at byte before offset)
3. If mid-line, skip the partial line remainder
4. Read and parse new complete lines
5. Return new entries + updated byte offset

### Collector Integration

`collect_once()` runs 3 additional scan passes after transcript indexing:

1. **Memory scan**: `scan_memory_files()` finds `*.md` files in each project's `memory/` dir
2. **Plans scan**: `scan_plans_dir()` reads `~/.claude/plans/`, resolves session IDs via codename index
3. **History scan**: `parse_history()` incrementally reads new entries from `history.jsonl`

## Error Handling Patterns

### Graceful Fallbacks

```python
# 1. Redis unavailable? Use filesystem
# 2. Anthropic API down? Use LLM_CHAT_MODEL
# 3. Postgres down? Disable semantic search
# 4. Lock acquisition fails? Skip (will retry next cycle)
```

### Structured Errors

```python
class AgentiBridgeError(Exception):
    """Base exception with structured context."""
    def __init__(self, message: str, context: dict | None = None):
        self.message = message
        self.context = context or {}
        super().__init__(message)

# Usage
raise SessionNotFoundError(
    "Session not found",
    context={"session_id": session_id, "project": project_path}
)
```

## Performance Characteristics

### Latency Targets

- `list_sessions`: < 100ms (Redis) or < 500ms (filesystem)
- `get_session`: < 50ms (cached) or < 200ms (uncached)
- `search_sessions`: < 500ms (keyword) or < 2s (semantic)
- `collect_now`: 1-5s (depends on transcript size)

### Memory Usage

- **Redis**: ~1KB per session metadata, ~100KB per cached transcript
- **Collector**: ~50MB baseline + ~1MB per 1000 transcript entries
- **Embeddings**: ~6KB per vector (1536 dims * 4 bytes)

### Scalability Limits

- **Sessions**: Tested with 10,000+ sessions
- **Transcripts**: Individual files up to 10MB (5,000+ entries)
- **Concurrent requests**: 100+ (SSE transport)

## Development Patterns

### Adding a New Tool

1. Add handler in `server.py`:
   ```python
   @mcp.tool()
   async def my_new_tool(arg: str) -> dict:
       """Tool description for MCP registry."""
       result = await store.do_something(arg)
       return {"result": result}
   ```

2. Update `store.py` with business logic
3. Add tests in `tests/unit/test_server.py`
4. Update documentation

### Adding Configuration

1. Add to `config.py`:
   ```python
   MY_NEW_VAR: str = os.getenv("MY_NEW_VAR", "default")
   ```

2. Add validation in `Config.__post_init__()`
3. Update `docs/reference/configuration.md`
4. Add to `agentibridge.env.example` generation in CLI

## See Also

- [Configuration Reference](../reference/configuration.md)
- [Semantic Search Details](semantic-search.md)
- [Remote Access Setup](remote-access.md)
- [Session Dispatch](session-dispatch.md)
- [Knowledge Catalog](knowledge-catalog.md)
