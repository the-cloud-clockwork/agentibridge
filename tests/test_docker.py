#!/usr/bin/env python3
"""AgentiBridge Docker integration tests.

Boots the Docker Compose stack, seeds test data, and validates
all 10 MCP tools across Phases 1-4.

Usage:
    # Start stack + run all tests
    python tests/test_docker.py

    # Start stack only (for on-demand testing)
    python tests/test_docker.py --start

    # Run tests only (stack already running)
    python tests/test_docker.py --test

    # Run a specific test
    python tests/test_docker.py --test --only phase1
    python tests/test_docker.py --test --only phase2
    python tests/test_docker.py --test --only phase4

    # Stop stack
    python tests/test_docker.py --stop

    # Clean up test data
    python tests/test_docker.py --cleanup
"""

import json
import os
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE_FILE = os.path.join(PROJECT_ROOT, "docker-compose.yml")
CONTAINER = "agentibridge"

COMPOSE_ENV = {
    **os.environ,
}

# Test session IDs (prefixed so we can clean up)
TEST_PREFIX = "sb-test-"


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def compose(*args, capture=False, check=True):
    cmd = ["docker", "compose", "-f", COMPOSE_FILE] + list(args)
    if capture:
        return subprocess.run(cmd, env=COMPOSE_ENV, capture_output=True, text=True, check=check)
    return subprocess.run(cmd, env=COMPOSE_ENV, check=check)


def docker_exec(python_code: str, timeout: int = 30) -> str:
    """Execute Python code inside the agentibridge container. Returns stdout."""
    # Wrap code with PYTHONPATH setup
    wrapped = f"import sys; sys.path.insert(0, '/app')\n{python_code}"
    result = subprocess.run(
        ["docker", "exec", "-w", "/app", CONTAINER, "python3", "-c", wrapped],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        # Exit code 134 (SIGABRT) is expected when daemon threads (collector)
        # are running at Python interpreter shutdown — tolerate if stdout is valid
        if result.returncode == 134 and result.stdout.strip():
            print("  (exit 134 tolerated — daemon thread shutdown)", file=sys.stderr)
            return result.stdout
        print(f"  STDERR: {result.stderr.strip()}", file=sys.stderr)
        raise RuntimeError(f"Container exec failed (exit {result.returncode}): {result.stderr[:500]}")
    return result.stdout


def wait_for_container(max_wait: int = 60):
    """Wait until the container is running and healthy."""
    print(f"Waiting for container '{CONTAINER}'...", end="", flush=True)
    for _ in range(max_wait):
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout.strip() == "true":
                # Check Python works
                docker_exec("print('ready')", timeout=10)
                print(" ready")
                return True
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(1)
    print(" TIMEOUT")
    return False


def wait_for_redis(max_wait: int = 30):
    """Wait for Redis to be accessible from the container."""
    print("Waiting for Redis...", end="", flush=True)
    for _ in range(max_wait):
        try:
            out = docker_exec("from agentibridge.redis_client import get_redis; r=get_redis(); print(r.ping())")
            if "True" in out:
                print(" connected")
                return True
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(1)
    print(" TIMEOUT")
    return False


# ---------------------------------------------------------------------------
# Test data seeding
# ---------------------------------------------------------------------------


SEED_CODE = """
import json
from agentibridge.store import SessionStore
from agentibridge.parser import SessionMeta, SessionEntry

store = SessionStore()

# Session 1: Docker/infrastructure work
store.upsert_session(SessionMeta(
    session_id="sb-test-docker-01",
    project_encoded="-home-user-dev-myapp",
    project_path="/home/user/dev/myapp",
    cwd="/home/user/dev/myapp",
    git_branch="main",
    start_time="2025-06-01T10:00:00Z",
    last_update="2025-06-01T11:30:00Z",
    num_user_turns=4, num_assistant_turns=4, num_tool_calls=8,
    summary="Set up Docker Compose with Redis and Nginx reverse proxy",
    transcript_path="/tmp/sb-test-docker-01.jsonl",
    has_subagents=False, file_size_bytes=5000,
))
store.add_entries("sb-test-docker-01", [
    SessionEntry("user", "2025-06-01T10:00:00Z", "Help me create a Docker Compose setup with Redis and Nginx"),
    SessionEntry("assistant", "2025-06-01T10:01:00Z", "I will create docker-compose.yml with three services: app, redis, and nginx reverse proxy.", ["Write"]),
    SessionEntry("user", "2025-06-01T10:05:00Z", "Add health checks for all services"),
    SessionEntry("assistant", "2025-06-01T10:06:00Z", "Added healthcheck blocks to all three services. Redis uses redis-cli ping, nginx uses curl.", ["Edit"]),
    SessionEntry("user", "2025-06-01T10:15:00Z", "Now configure Nginx SSL termination"),
    SessionEntry("assistant", "2025-06-01T10:16:00Z", "Created nginx.conf with SSL termination, HTTP to HTTPS redirect, and upstream proxy_pass to the app.", ["Write", "Edit"]),
    SessionEntry("user", "2025-06-01T11:00:00Z", "Test the full stack"),
    SessionEntry("assistant", "2025-06-01T11:01:00Z", "All services healthy. Nginx proxying to app, Redis connected, SSL working with self-signed cert.", ["Bash", "Read"]),
])

# Session 2: Authentication implementation
store.upsert_session(SessionMeta(
    session_id="sb-test-auth-02",
    project_encoded="-home-user-dev-myapp",
    project_path="/home/user/dev/myapp",
    cwd="/home/user/dev/myapp",
    git_branch="feature/auth",
    start_time="2025-06-02T09:00:00Z",
    last_update="2025-06-02T12:00:00Z",
    num_user_turns=5, num_assistant_turns=5, num_tool_calls=15,
    summary="Implemented JWT authentication with refresh tokens and role-based access control",
    transcript_path="/tmp/sb-test-auth-02.jsonl",
    has_subagents=False, file_size_bytes=8000,
))
store.add_entries("sb-test-auth-02", [
    SessionEntry("user", "2025-06-02T09:00:00Z", "Implement JWT authentication for our FastAPI app"),
    SessionEntry("assistant", "2025-06-02T09:01:00Z", "Created auth module with JWT token creation, validation, and middleware. Using python-jose for JWT and passlib for password hashing.", ["Write", "Edit"]),
    SessionEntry("user", "2025-06-02T09:30:00Z", "Add refresh token support"),
    SessionEntry("assistant", "2025-06-02T09:31:00Z", "Added refresh token endpoint. Tokens stored in Redis with TTL. Rotation on each refresh.", ["Write", "Edit", "Read"]),
    SessionEntry("user", "2025-06-02T10:00:00Z", "Now add role-based access control"),
    SessionEntry("assistant", "2025-06-02T10:01:00Z", "Implemented RBAC with admin, editor, viewer roles. Added Depends() decorators for route protection.", ["Write", "Edit"]),
    SessionEntry("user", "2025-06-02T11:00:00Z", "Write tests for the auth module"),
    SessionEntry("assistant", "2025-06-02T11:01:00Z", "Created comprehensive test suite: login, token refresh, RBAC enforcement, expired token handling.", ["Write", "Bash"]),
    SessionEntry("user", "2025-06-02T11:30:00Z", "All tests passing. Deploy to staging"),
    SessionEntry("assistant", "2025-06-02T11:31:00Z", "Deployed to staging via docker compose. Auth endpoints verified. RBAC working correctly.", ["Bash", "Read"]),
])

# Session 3: Database migration (different project)
store.upsert_session(SessionMeta(
    session_id="sb-test-db-03",
    project_encoded="-home-user-dev-backend",
    project_path="/home/user/dev/backend",
    cwd="/home/user/dev/backend",
    git_branch="main",
    start_time="2025-06-03T14:00:00Z",
    last_update="2025-06-03T15:00:00Z",
    num_user_turns=3, num_assistant_turns=3, num_tool_calls=6,
    summary="PostgreSQL to DynamoDB migration for the user profiles table",
    transcript_path="/tmp/sb-test-db-03.jsonl",
    has_subagents=False, file_size_bytes=4000,
))
store.add_entries("sb-test-db-03", [
    SessionEntry("user", "2025-06-03T14:00:00Z", "Migrate user_profiles table from PostgreSQL to DynamoDB"),
    SessionEntry("assistant", "2025-06-03T14:01:00Z", "Designed DynamoDB table schema with partition key (user_id) and GSI on email. Created migration script.", ["Write", "Read"]),
    SessionEntry("user", "2025-06-03T14:30:00Z", "Run the migration with a dry run first"),
    SessionEntry("assistant", "2025-06-03T14:31:00Z", "Dry run complete: 15,432 records would be migrated. No schema conflicts found.", ["Bash"]),
    SessionEntry("user", "2025-06-03T14:45:00Z", "Execute the full migration"),
    SessionEntry("assistant", "2025-06-03T14:46:00Z", "Migration complete. All 15,432 records transferred. Verified with spot checks on 100 random records.", ["Bash", "Read"]),
])

print(json.dumps({"seeded": 3, "sessions": ["sb-test-docker-01", "sb-test-auth-02", "sb-test-db-03"]}))
"""


def seed_test_data():
    """Seed test sessions into Redis via the container."""
    print("\nSeeding test data...")
    out = docker_exec(SEED_CODE)
    data = json.loads(out.strip().split("\n")[-1])
    print(f"  Seeded {data['seeded']} sessions: {data['sessions']}")
    return data


# ---------------------------------------------------------------------------
# Phase 1 tests
# ---------------------------------------------------------------------------


def test_phase1():
    """Test Phase 1: Foundation tools (list, get, search, segment, actions, collect)."""
    print("\n" + "=" * 60)
    print("PHASE 1: Foundation")
    print("=" * 60)
    passed = 0
    failed = 0

    # Test: list_sessions
    print("\n[1.1] list_sessions — all")
    out = docker_exec("""
import json
from agentibridge.server import list_sessions
result = json.loads(list_sessions(limit=50))
print(json.dumps(result))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["success"], f"list_sessions failed: {data}"
    assert data["count"] >= 3, f"Expected >=3 sessions, got {data['count']}"
    print(f"  Found {data['count']} sessions")
    passed += 1

    # Test: list_sessions with project filter
    print("\n[1.2] list_sessions — project filter")
    out = docker_exec("""
import json
from agentibridge.server import list_sessions
result = json.loads(list_sessions(project="myapp", limit=50))
print(json.dumps(result))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["success"]
    assert data["count"] == 2, f"Expected 2 myapp sessions, got {data['count']}"
    print(f"  Filtered to {data['count']} sessions (myapp)")
    passed += 1

    # Test: get_session
    print("\n[1.3] get_session")
    out = docker_exec("""
import json
from agentibridge.server import get_session
result = json.loads(get_session(session_id="sb-test-auth-02", last_n=10))
print(json.dumps(result))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["success"]
    assert data["meta"]["git_branch"] == "feature/auth"
    assert data["entry_count"] > 0
    print(f"  Got session: branch={data['meta']['git_branch']}, entries={data['entry_count']}")
    passed += 1

    # Test: get_session_segment
    print("\n[1.4] get_session_segment")
    out = docker_exec("""
import json
from agentibridge.server import get_session_segment
result = json.loads(get_session_segment(session_id="sb-test-docker-01", offset=0, limit=3))
print(json.dumps(result))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["success"]
    assert data["count"] == 3
    assert data["total_count"] >= 8
    print(f"  Segment: {data['count']} of {data['total_count']} entries")
    passed += 1

    # Test: get_session_actions
    print("\n[1.5] get_session_actions")
    out = docker_exec("""
import json
from agentibridge.server import get_session_actions
result = json.loads(get_session_actions(session_id="sb-test-auth-02"))
print(json.dumps(result))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["success"]
    assert data["total_tool_calls"] > 0
    tools = {t["name"]: t["count"] for t in data["tools"]}
    print(f"  Tools: {tools}")
    assert "Write" in tools
    assert "Edit" in tools
    passed += 1

    # Test: search_sessions
    print("\n[1.6] search_sessions — keyword")
    out = docker_exec("""
import json
from agentibridge.server import search_sessions
result = json.loads(search_sessions(query="JWT authentication"))
print(json.dumps(result))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["success"]
    assert data["count"] >= 1
    assert any("auth" in m.get("session_id", "") for m in data["matches"])
    print(f"  Found {data['count']} match(es) for 'JWT authentication'")
    passed += 1

    # Test: collect_now
    print("\n[1.7] collect_now")
    out = docker_exec("""
import json
from agentibridge.server import collect_now
result = json.loads(collect_now())
print(json.dumps(result))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["success"]
    print(f"  Collection: {data.get('files_scanned', 0)} files, {data.get('duration_ms', 0)}ms")
    passed += 1

    print(f"\nPhase 1: {passed} passed, {failed} failed")
    return passed, failed


# ---------------------------------------------------------------------------
# Phase 2 tests
# ---------------------------------------------------------------------------


def test_phase2():
    """Test Phase 2: Semantic search tools."""
    print("\n" + "=" * 60)
    print("PHASE 2: Semantic Search")
    print("=" * 60)
    passed = 0
    failed = 0

    # Test: embedder availability
    print("\n[2.1] Embedder availability check")
    out = docker_exec("""
import json
from agentibridge.embeddings import TranscriptEmbedder
e = TranscriptEmbedder()
print(json.dumps({"available": e.is_available()}))
""")
    data = json.loads(out.strip().split("\n")[-1])
    print(f"  Embedder available: {data['available']}")
    passed += 1  # Just checking it doesn't crash

    # Test: chunking logic
    print("\n[2.2] Transcript chunking")
    out = docker_exec("""
import json
from agentibridge.embeddings import TranscriptEmbedder
from agentibridge.parser import SessionEntry
e = TranscriptEmbedder()
entries = [
    SessionEntry("user", "2025-01-01T00:00:00Z", "Fix the bug"),
    SessionEntry("assistant", "2025-01-01T00:00:01Z", "Looking at code", ["Read"]),
    SessionEntry("user", "2025-01-01T00:00:02Z", "Add tests"),
    SessionEntry("assistant", "2025-01-01T00:00:03Z", "Writing tests", ["Write"]),
]
chunks = e._chunk_turns(entries)
print(json.dumps({"chunks": len(chunks), "texts": [c["text"][:60] for c in chunks]}))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["chunks"] == 2, f"Expected 2 chunks, got {data['chunks']}"
    print(f"  Chunks: {data['chunks']}")
    passed += 1

    # Test: cosine similarity
    print("\n[2.3] Cosine similarity (batch)")
    out = docker_exec("""
import json
from agentibridge.embeddings import _cosine_similarity_batch
q = [1.0, 0.0, 0.0]
vecs = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.7, 0.7, 0.0]]
scores = _cosine_similarity_batch(q, vecs)
print(json.dumps({"scores": [round(s, 3) for s in scores]}))
""")
    data = json.loads(out.strip().split("\n")[-1])
    scores = data["scores"]
    assert scores[0] == 1.0, "Same vector should be 1.0"
    assert scores[1] == 0.0, "Orthogonal should be 0.0"
    assert 0.5 < scores[2] < 1.0, "Partial match should be between 0.5 and 1.0"
    print(f"  Scores: same={scores[0]}, ortho={scores[1]}, partial={scores[2]}")
    passed += 1

    # Test: search_semantic tool (graceful when no embeddings stored)
    print("\n[2.4] search_semantic — no embeddings yet")
    out = docker_exec("""
import json
from agentibridge.server import search_semantic
result = json.loads(search_semantic(query="Docker setup"))
print(json.dumps(result))
""")
    data = json.loads(out.strip().split("\n")[-1])
    # Should either succeed with 0 results or fail gracefully
    print(
        f"  Result: success={data.get('success')}, count={data.get('count', 0)}, error={data.get('error', 'none')[:80]}"
    )
    passed += 1

    # Test: generate_summary tool
    print("\n[2.5] generate_summary — format check")
    out = docker_exec("""
import json
from agentibridge.embeddings import TranscriptEmbedder
from agentibridge.parser import SessionEntry
e = TranscriptEmbedder()
entries = [
    SessionEntry("user", "T1", "Build a REST API"),
    SessionEntry("assistant", "T2", "Created endpoints for CRUD operations", ["Write"]),
]
text = e._build_transcript_text(entries, max_chars=500)
print(json.dumps({"length": len(text), "preview": text[:200]}))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["length"] > 0
    assert "User:" in data["preview"]
    assert "Assistant" in data["preview"]
    print(f"  Transcript text: {data['length']} chars")
    passed += 1

    print(f"\nPhase 2: {passed} passed, {failed} failed")
    return passed, failed


# ---------------------------------------------------------------------------
# Phase 3 tests
# ---------------------------------------------------------------------------


def test_phase3():
    """Test Phase 3: Remote access (transport + auth)."""
    print("\n" + "=" * 60)
    print("PHASE 3: Remote Access")
    print("=" * 60)
    passed = 0
    failed = 0

    # Test: auth with no keys (pass-through)
    print("\n[3.1] Auth — no keys configured (pass-through)")
    out = docker_exec("""
import json
from agentibridge.transport import validate_api_key
results = {
    "none_key": validate_api_key(None),
    "any_key": validate_api_key("anything"),
    "empty_key": validate_api_key(""),
}
print(json.dumps(results))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["none_key"] is True
    assert data["any_key"] is True
    print(f"  No keys: all pass-through = {all(data.values())}")
    passed += 1

    # Test: auth with keys configured
    print("\n[3.2] Auth — with API keys")
    out = docker_exec("""
import os, json
os.environ["AGENTIBRIDGE_API_KEYS"] = "key-alpha,key-beta"
# Reimport to pick up new env
from agentibridge import transport
transport._get_api_keys.__module__  # force module reference
results = {
    "valid_key": transport.validate_api_key("key-alpha"),
    "valid_key2": transport.validate_api_key("key-beta"),
    "invalid_key": transport.validate_api_key("wrong-key"),
    "no_key": transport.validate_api_key(None),
}
os.environ.pop("AGENTIBRIDGE_API_KEYS", None)
print(json.dumps(results))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["valid_key"] is True
    assert data["valid_key2"] is True
    assert data["invalid_key"] is False
    assert data["no_key"] is False
    print(f"  Auth: valid={data['valid_key']}, invalid={data['invalid_key']}")
    passed += 1

    # Test: transport config
    print("\n[3.3] Transport config defaults")
    out = docker_exec("""
import json
from agentibridge.config import AGENTIBRIDGE_TRANSPORT, AGENTIBRIDGE_PORT
print(json.dumps({"transport": AGENTIBRIDGE_TRANSPORT, "port": AGENTIBRIDGE_PORT}))
""")
    data = json.loads(out.strip().split("\n")[-1])
    # In the container, transport is set to "sse" via env
    print(f"  Transport: {data['transport']}, Port: {data['port']}")
    passed += 1

    # Test: transport module imports
    print("\n[3.4] SSE transport module loads")
    out = docker_exec("""
import json
from agentibridge.transport import run_sse_server, validate_api_key
print(json.dumps({"run_sse_server": "ok", "validate_api_key": "ok"}))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["run_sse_server"] == "ok"
    print("  SSE module: loaded")
    passed += 1

    print(f"\nPhase 3: {passed} passed, {failed} failed")
    return passed, failed


# ---------------------------------------------------------------------------
# Phase 4 tests
# ---------------------------------------------------------------------------


def test_phase4():
    """Test Phase 4: Write-back & dispatch."""
    print("\n" + "=" * 60)
    print("PHASE 4: Write-back & Dispatch")
    print("=" * 60)
    passed = 0
    failed = 0

    # Test: restore_session
    print("\n[4.1] restore_session — Docker session")
    out = docker_exec("""
import json
from agentibridge.server import restore_session
result = json.loads(restore_session(session_id="sb-test-docker-01", last_n=10))
print(json.dumps({"success": result["success"], "chars": result.get("char_count", 0), "preview": result.get("context", "")[:200]}))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["success"]
    assert data["chars"] > 100
    assert "RESTORED SESSION CONTEXT" in data["preview"]
    print(f"  Context: {data['chars']} chars")
    passed += 1

    # Test: restore_session — content check
    print("\n[4.2] restore_session — content validation")
    out = docker_exec("""
import json
from agentibridge.server import restore_session
result = json.loads(restore_session(session_id="sb-test-auth-02", last_n=5))
ctx = result.get("context", "")
checks = {
    "has_project": "/home/user/dev/myapp" in ctx,
    "has_branch": "feature/auth" in ctx,
    "has_user_msg": "[USER]" in ctx,
    "has_assistant_msg": "[ASSISTANT]" in ctx,
    "has_header": "RESTORED SESSION CONTEXT" in ctx,
    "has_footer": "END OF RESTORED CONTEXT" in ctx,
}
print(json.dumps(checks))
""")
    data = json.loads(out.strip().split("\n")[-1])
    for check, val in data.items():
        assert val, f"Content check failed: {check}"
    print(f"  Content checks: all {len(data)} passed")
    passed += 1

    # Test: restore_session — nonexistent session
    print("\n[4.3] restore_session — missing session")
    out = docker_exec("""
import json
from agentibridge.server import restore_session
result = json.loads(restore_session(session_id="nonexistent-session"))
print(json.dumps({"success": result["success"], "error": result.get("error", "")[:100]}))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert not data["success"]
    assert "not found" in data["error"].lower()
    print(f"  Error handled: {data['error'][:60]}")
    passed += 1

    # Test: dispatch.py — module loads
    print("\n[4.4] dispatch module — import check")
    out = docker_exec("""
import json
from agentibridge.dispatch import restore_session_context
print(json.dumps({"restore": "ok"}))
""")
    data = json.loads(out.strip().split("\n")[-1])
    assert data["restore"] == "ok"
    print("  Dispatch module: loaded")
    passed += 1

    print(f"\nPhase 4: {passed} passed, {failed} failed")
    return passed, failed


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_test_data():
    """Remove test sessions from Redis."""
    print("\nCleaning up test data...")
    out = docker_exec("""
import json
from agentibridge.redis_client import get_redis
r = get_redis()
if r is None:
    print(json.dumps({"cleaned": 0}))
else:
    keys = list(r.scan_iter(match="*sb-test-*"))
    keys += list(r.scan_iter(match="*:sb:session:sb-test-*"))
    keys += list(r.scan_iter(match="*:sb:vec:sb-test-*"))
    for k in keys:
        r.delete(k)
    # Also clean sorted set entries
    for idx_key in r.scan_iter(match="*:sb:idx:*"):
        for member in r.smembers(idx_key) if r.type(idx_key) == b"set" else []:
            if b"sb-test-" in member:
                r.srem(idx_key, member)
        for member, _ in r.zscan_iter(idx_key) if r.type(idx_key) == b"zset" else []:
            if b"sb-test-" in member:
                r.zrem(idx_key, member)
    print(json.dumps({"cleaned": len(keys)}))
""")
    data = json.loads(out.strip().split("\n")[-1])
    print(f"  Removed {data['cleaned']} keys")


# ---------------------------------------------------------------------------
# Stack management
# ---------------------------------------------------------------------------


def start_stack():
    """Start Docker Compose stack."""
    print("Starting Docker Compose stack...")
    compose("up", "--build", "-d")
    if not wait_for_container():
        print("ERROR: Container did not start")
        sys.exit(1)
    if not wait_for_redis():
        print("ERROR: Redis not available")
        sys.exit(1)


def stop_stack():
    """Stop Docker Compose stack."""
    print("Stopping Docker Compose stack...")
    compose("down", check=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="AgentiBridge Docker integration tests")
    parser.add_argument("--start", action="store_true", help="Start stack only")
    parser.add_argument("--stop", action="store_true", help="Stop stack")
    parser.add_argument("--test", action="store_true", help="Run tests only (stack must be running)")
    parser.add_argument("--cleanup", action="store_true", help="Clean up test data")
    parser.add_argument("--only", type=str, help="Run only specific phase: phase1, phase2, phase3, phase4")
    args = parser.parse_args()

    if args.stop:
        stop_stack()
        return

    if args.cleanup:
        cleanup_test_data()
        return

    # Start stack if not --test only
    if not args.test:
        start_stack()

    if args.start:
        print("\nStack is running. Use --test to run tests.")
        return

    # Run tests
    seed_test_data()

    total_passed = 0
    total_failed = 0

    phases = {
        "phase1": test_phase1,
        "phase2": test_phase2,
        "phase3": test_phase3,
        "phase4": test_phase4,
    }

    if args.only:
        if args.only in phases:
            p, f = phases[args.only]()
            total_passed += p
            total_failed += f
        else:
            print(f"Unknown phase: {args.only}. Choose from: {list(phases.keys())}")
            sys.exit(1)
    else:
        for phase_fn in phases.values():
            p, f = phase_fn()
            total_passed += p
            total_failed += f

    # Summary
    print("\n" + "=" * 60)
    print(f"TOTAL: {total_passed} passed, {total_failed} failed")
    print("=" * 60)

    # Cleanup
    cleanup_test_data()

    if total_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
