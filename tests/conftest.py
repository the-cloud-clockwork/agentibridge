"""Shared test fixtures for agentibridge."""

import json
import shutil
from pathlib import Path

import pytest

from agentibridge.catalog import HistoryEntry, MemoryFile
from agentibridge.parser import SessionEntry, SessionMeta


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_TRANSCRIPT = FIXTURES_DIR / "sample_transcript.jsonl"
MALFORMED_TRANSCRIPT = FIXTURES_DIR / "malformed_transcript.jsonl"


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def make_entry(
    entry_type: str = "user",
    timestamp: str = "2025-06-01T10:00:00Z",
    content: str = "Hello",
    tool_names: list = None,
    uuid: str = "test-uuid",
) -> SessionEntry:
    return SessionEntry(
        entry_type=entry_type,
        timestamp=timestamp,
        content=content,
        tool_names=tool_names or [],
        uuid=uuid,
    )


def make_meta(
    session_id: str = "test-session-001",
    project_encoded: str = "-home-user-dev-myapp",
    project_path: str = "/home/user/dev/myapp",
    cwd: str = "/home/user/dev/myapp",
    git_branch: str = "main",
    start_time: str = "2025-06-01T10:00:00Z",
    last_update: str = "2025-06-01T11:00:00Z",
    num_user_turns: int = 5,
    num_assistant_turns: int = 5,
    num_tool_calls: int = 10,
    summary: str = "Test session summary",
    transcript_path: str = "/tmp/test.jsonl",
    has_subagents: bool = False,
    file_size_bytes: int = 5000,
    codename: str = "",
) -> SessionMeta:
    return SessionMeta(
        session_id=session_id,
        project_encoded=project_encoded,
        project_path=project_path,
        cwd=cwd,
        git_branch=git_branch,
        start_time=start_time,
        last_update=last_update,
        num_user_turns=num_user_turns,
        num_assistant_turns=num_assistant_turns,
        num_tool_calls=num_tool_calls,
        summary=summary,
        transcript_path=transcript_path,
        has_subagents=has_subagents,
        file_size_bytes=file_size_bytes,
        codename=codename,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_entries():
    """A list of sample SessionEntry objects."""
    return [
        make_entry("user", "2025-06-01T10:00:00Z", "Create a REST API", uuid="e1"),
        make_entry("assistant", "2025-06-01T10:01:00Z", "Created CRUD endpoints", ["Write", "Edit"], "e2"),
        make_entry("user", "2025-06-01T10:05:00Z", "Add authentication", uuid="e3"),
        make_entry("assistant", "2025-06-01T10:06:00Z", "Added JWT auth", ["Write"], "e4"),
        make_entry("summary", "2025-06-01T10:10:00Z", "Built REST API with JWT auth", uuid="e5"),
    ]


@pytest.fixture
def sample_meta():
    """A sample SessionMeta object."""
    return make_meta()


@pytest.fixture
def temp_projects_dir(tmp_path):
    """Create a temporary projects directory with sample transcripts.

    Structure:
        tmp_path/
            -home-user-dev-myapp/
                session-001.jsonl
            -home-user-dev-backend/
                session-002.jsonl
    """
    proj1 = tmp_path / "-home-user-dev-myapp"
    proj1.mkdir()
    shutil.copy(SAMPLE_TRANSCRIPT, proj1 / "session-001.jsonl")

    proj2 = tmp_path / "-home-user-dev-backend"
    proj2.mkdir()
    # Write a minimal transcript for the second project
    with open(proj2 / "session-002.jsonl", "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2025-06-02T09:00:00Z",
                    "uuid": "b1",
                    "cwd": "/home/user/dev/backend",
                    "gitBranch": "develop",
                    "message": {"role": "user", "content": "Migrate database"},
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2025-06-02T09:01:00Z",
                    "uuid": "b2",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Running migration script."},
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "Bash",
                                "input": {"command": "alembic upgrade head"},
                            },
                        ],
                    },
                }
            )
            + "\n"
        )

    return tmp_path


@pytest.fixture
def mock_redis():
    """Provide a fakeredis instance and patch get_redis to return it."""
    import fakeredis

    fake = fakeredis.FakeRedis(decode_responses=True)

    import agentibridge.redis_client as rc

    original_client = rc._redis_client
    original_checked = rc._redis_checked

    rc._redis_client = fake
    rc._redis_checked = True

    yield fake

    # Restore
    rc._redis_client = original_client
    rc._redis_checked = original_checked
    fake.flushall()


@pytest.fixture
def reset_singletons():
    """Reset module-level singletons used by server.py."""
    import agentibridge.server as srv

    # Save
    old_store = srv._store
    old_collector = srv._collector
    old_embedder = srv._embedder

    srv._store = None
    srv._collector = None
    srv._embedder = None

    yield

    # Restore
    srv._store = old_store
    srv._collector = old_collector
    srv._embedder = old_embedder


# ---------------------------------------------------------------------------
# Phase 5 — Catalog factory helpers
# ---------------------------------------------------------------------------


def make_memory_file(
    project_encoded: str = "-home-user-dev-myapp",
    project_path: str = "/home/user/dev/myapp",
    filename: str = "MEMORY.md",
    filepath: str = "/tmp/memory/MEMORY.md",
    content: str = "# Project Memory\n\nKey decisions go here.",
    file_size_bytes: int = 128,
    last_modified: str = "2025-06-01T10:00:00+00:00",
) -> MemoryFile:
    return MemoryFile(
        project_encoded=project_encoded,
        project_path=project_path,
        filename=filename,
        filepath=filepath,
        content=content,
        file_size_bytes=file_size_bytes,
        last_modified=last_modified,
    )


def make_history_entry(
    display: str = "Help me create a Docker Compose setup",
    timestamp: str = "2025-06-01T10:00:00+00:00",
    project: str = "/home/user/dev/myapp",
    session_id: str = "test-session-001",
) -> HistoryEntry:
    return HistoryEntry(
        display=display,
        timestamp=timestamp,
        project=project,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Phase 5 — Catalog fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_memory_dir(tmp_path):
    """Create a temporary projects directory with memory files.

    Structure:
        tmp_path/
            -home-user-dev-myapp/
                memory/
                    MEMORY.md
                    patterns.md
            -home-user-dev-backend/
                memory/
                    MEMORY.md
    """
    proj1 = tmp_path / "-home-user-dev-myapp" / "memory"
    proj1.mkdir(parents=True)
    (proj1 / "MEMORY.md").write_text("# MyApp Memory\n\nKey patterns here.\n")
    (proj1 / "patterns.md").write_text("# Patterns\n\n- Use factory pattern\n")

    proj2 = tmp_path / "-home-user-dev-backend" / "memory"
    proj2.mkdir(parents=True)
    (proj2 / "MEMORY.md").write_text("# Backend Memory\n\nAPI design notes.\n")

    return tmp_path


@pytest.fixture
def temp_history_file(tmp_path):
    """Create a temporary history.jsonl with sample entries."""
    history_file = tmp_path / "history.jsonl"
    entries = [
        {
            "display": "Help me create a Docker Compose setup",
            "timestamp": 1717236000000,  # 2024-06-01T10:00:00Z
            "projectPath": "/home/user/dev/myapp",
            "sessionId": "session-001",
        },
        {
            "display": "Add health checks to services",
            "timestamp": 1717236300000,  # +5min
            "projectPath": "/home/user/dev/myapp",
            "sessionId": "session-001",
        },
        {
            "display": "Migrate the database schema",
            "timestamp": 1717236600000,  # +10min
            "projectPath": "/home/user/dev/backend",
            "sessionId": "session-002",
        },
        {
            "display": "Fix the authentication bug",
            "timestamp": 1717236900000,  # +15min
            "projectPath": "/home/user/dev/backend",
            "sessionId": "session-003",
        },
        {
            "display": "Deploy to production",
            "timestamp": 1717237200000,  # +20min
            "projectPath": "/home/user/dev/myapp",
            "sessionId": "session-001",
        },
    ]
    with open(history_file, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return history_file
