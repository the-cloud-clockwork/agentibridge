"""Tests for agentibridge.server module — all 10 MCP tools."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_entry, make_meta


def _mock_store(sessions=None, entries=None, search_results=None, count=0):
    """Create a mock SessionStore."""
    store = MagicMock()
    store.list_sessions.return_value = sessions or []
    store.get_session_meta.return_value = sessions[0] if sessions else None
    store.get_session_entries.return_value = entries or []
    store.search_sessions.return_value = search_results or []
    store.count_entries.return_value = count
    return store


def _mock_collector():
    collector = MagicMock()
    collector.collect_once.return_value = {
        "files_scanned": 5,
        "sessions_updated": 2,
        "entries_added": 10,
        "duration_ms": 50,
    }
    return collector


@pytest.mark.unit
class TestListSessions:
    def test_success(self, reset_singletons):
        meta = make_meta()
        store = _mock_store(sessions=[meta])
        collector = _mock_collector()

        import agentibridge.server as srv

        srv._store = store
        srv._collector = collector

        result = json.loads(srv.list_sessions(limit=20))

        assert result["success"] is True
        assert result["count"] == 1
        assert result["sessions"][0]["session_id"] == "test-session-001"

    def test_with_project_filter(self, reset_singletons):
        store = _mock_store(sessions=[])
        collector = _mock_collector()

        import agentibridge.server as srv

        srv._store = store
        srv._collector = collector

        result = json.loads(srv.list_sessions(project="myapp"))

        assert result["success"] is True
        store.list_sessions.assert_called_once_with(
            project="myapp",
            limit=20,
            offset=0,
            since_hours=0,
        )

    def test_error_handling(self, reset_singletons):
        store = MagicMock()
        store.list_sessions.side_effect = Exception("db error")
        collector = _mock_collector()

        import agentibridge.server as srv

        srv._store = store
        srv._collector = collector

        result = json.loads(srv.list_sessions())
        assert result["success"] is False
        assert "db error" in result["error"]


@pytest.mark.unit
class TestGetSession:
    def test_success(self, reset_singletons):
        meta = make_meta()
        entries = [make_entry("user", content="Hello"), make_entry("assistant", content="Hi")]
        store = _mock_store(sessions=[meta], entries=entries, count=2)

        import agentibridge.server as srv

        srv._store = store
        srv._collector = _mock_collector()

        result = json.loads(srv.get_session(session_id="test-session-001", last_n=50))

        assert result["success"] is True
        assert "meta" in result
        assert result["entry_count"] == 2

    def test_session_not_found(self, reset_singletons):
        store = MagicMock()
        store.get_session_meta.return_value = None

        import agentibridge.server as srv

        srv._store = store
        srv._collector = _mock_collector()

        result = json.loads(srv.get_session(session_id="nonexistent"))
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_all_entries(self, reset_singletons):
        meta = make_meta()
        entries = [make_entry() for _ in range(10)]
        store = _mock_store(sessions=[meta], entries=entries, count=10)

        import agentibridge.server as srv

        srv._store = store
        srv._collector = _mock_collector()

        result = json.loads(srv.get_session(session_id="test-session-001", last_n=0))
        assert result["success"] is True


@pytest.mark.unit
class TestGetSessionSegment:
    def test_offset_limit(self, reset_singletons):
        entries = [make_entry() for _ in range(5)]
        store = _mock_store(entries=entries, count=20)

        import agentibridge.server as srv

        srv._store = store
        srv._collector = _mock_collector()

        result = json.loads(
            srv.get_session_segment(
                session_id="test-session-001",
                offset=0,
                limit=5,
            )
        )

        assert result["success"] is True
        assert result["count"] == 5
        assert result["total_count"] == 20

    def test_time_range(self, reset_singletons):
        entries = [
            make_entry(timestamp="2025-06-01T10:00:00Z"),
            make_entry(timestamp="2025-06-01T11:00:00Z"),
            make_entry(timestamp="2025-06-01T12:00:00Z"),
        ]
        store = _mock_store(entries=entries, count=3)

        import agentibridge.server as srv

        srv._store = store
        srv._collector = _mock_collector()

        result = json.loads(
            srv.get_session_segment(
                session_id="test",
                since="2025-06-01T10:30:00Z",
                until="2025-06-01T11:30:00Z",
            )
        )

        assert result["success"] is True
        # Only the 11:00 entry matches
        assert result["count"] == 1


@pytest.mark.unit
class TestGetSessionActions:
    def test_tool_counts(self, reset_singletons):
        entries = [
            make_entry("assistant", tool_names=["Write", "Edit"]),
            make_entry("assistant", tool_names=["Write", "Bash"]),
            make_entry("assistant", tool_names=["Read"]),
        ]
        store = _mock_store(entries=entries)

        import agentibridge.server as srv

        srv._store = store
        srv._collector = _mock_collector()

        result = json.loads(srv.get_session_actions(session_id="test"))

        assert result["success"] is True
        assert result["total_tool_calls"] == 5
        assert result["unique_tools"] == 4
        tools = {t["name"]: t["count"] for t in result["tools"]}
        assert tools["Write"] == 2
        assert tools["Edit"] == 1


@pytest.mark.unit
class TestSearchSessions:
    def test_keyword_search(self, reset_singletons):
        store = _mock_store(
            search_results=[
                {
                    "session_id": "s1",
                    "project_path": "/path",
                    "entry_type": "user",
                    "content_preview": "JWT authentication",
                    "timestamp": "2025-06-01T10:00:00Z",
                }
            ]
        )

        import agentibridge.server as srv

        srv._store = store
        srv._collector = _mock_collector()

        result = json.loads(srv.search_sessions(query="JWT"))

        assert result["success"] is True
        assert result["count"] == 1
        assert result["query"] == "JWT"


@pytest.mark.unit
class TestCollectNow:
    def test_success(self, reset_singletons):
        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()

        result = json.loads(asyncio.run(srv.collect_now()))

        assert result["success"] is True
        assert result["files_scanned"] == 5


@pytest.mark.unit
class TestSearchSemantic:
    def test_unavailable_backend(self, reset_singletons):
        embedder = MagicMock()
        embedder.is_available.return_value = False

        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()
        srv._embedder = embedder

        result = json.loads(asyncio.run(srv.search_semantic(query="test")))
        assert result["success"] is False
        assert "not available" in result["error"].lower()

    def test_success(self, reset_singletons):
        embedder = MagicMock()
        embedder.is_available.return_value = True
        embedder.search_semantic.return_value = [{"session_id": "s1", "score": 0.95}]

        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()
        srv._embedder = embedder

        result = json.loads(asyncio.run(srv.search_semantic(query="Docker setup")))
        assert result["success"] is True
        assert result["count"] == 1


@pytest.mark.unit
class TestGenerateSummary:
    def test_success(self, reset_singletons):
        embedder = MagicMock()
        embedder.generate_summary.return_value = "Built a REST API with auth."

        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()
        srv._embedder = embedder

        result = json.loads(asyncio.run(srv.generate_summary(session_id="test")))
        assert result["success"] is True
        assert result["summary"] == "Built a REST API with auth."

    def test_error(self, reset_singletons):
        embedder = MagicMock()
        embedder.generate_summary.side_effect = Exception("API error")

        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()
        srv._embedder = embedder

        result = json.loads(asyncio.run(srv.generate_summary(session_id="test")))
        assert result["success"] is False


@pytest.mark.unit
class TestRestoreSession:
    def test_success(self, reset_singletons):
        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()

        with patch("agentibridge.dispatch.restore_session_context") as mock_restore:
            mock_restore.return_value = "RESTORED SESSION CONTEXT\nproject info\nentries"

            result = json.loads(asyncio.run(srv.restore_session(session_id="test", last_n=20)))

            assert result["success"] is True
            assert result["char_count"] > 0

    def test_not_found(self, reset_singletons):
        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()

        with patch("agentibridge.dispatch.restore_session_context") as mock_restore:
            mock_restore.side_effect = ValueError("Session not found: test")

            result = json.loads(asyncio.run(srv.restore_session(session_id="test")))
            assert result["success"] is False


@pytest.mark.unit
class TestRemoveTools:
    def test_removes_specified_tools(self, reset_singletons):
        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()

        # Verify tool exists before removal
        tool_names_before = [t.name for t in srv.mcp._tool_manager.list_tools()]
        assert "dispatch_task" in tool_names_before

        with patch.dict("os.environ", {"AGENTIBRIDGE_REMOVE_TOOLS": "dispatch_task,search_semantic"}):
            # Re-import to pick up the new env var
            import importlib
            import agentibridge.config

            importlib.reload(agentibridge.config)

            srv.main.__wrapped__ = None  # not needed, just call main logic
            from agentibridge.config import AGENTIBRIDGE_REMOVE_TOOLS

            for name in AGENTIBRIDGE_REMOVE_TOOLS:
                try:
                    srv.mcp._tool_manager.remove_tool(name)
                except Exception:
                    pass

            tool_names_after = [t.name for t in srv.mcp._tool_manager.list_tools()]
            assert "dispatch_task" not in tool_names_after
            assert "search_semantic" not in tool_names_after
            # Other tools should still be present
            assert "list_sessions" in tool_names_after

    def test_ignores_unknown_tools(self, reset_singletons):
        """Setting a non-existent tool name should not raise."""
        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()

        with patch.dict("os.environ", {"AGENTIBRIDGE_REMOVE_TOOLS": "nonexistent_tool"}):
            import importlib
            import agentibridge.config

            importlib.reload(agentibridge.config)

            from agentibridge.config import AGENTIBRIDGE_REMOVE_TOOLS

            for name in AGENTIBRIDGE_REMOVE_TOOLS:
                try:
                    srv.mcp._tool_manager.remove_tool(name)
                except Exception:
                    pass  # should not raise

            # All original tools should still be present
            tool_names = [t.name for t in srv.mcp._tool_manager.list_tools()]
            assert "list_sessions" in tool_names

    def test_empty_env_removes_nothing(self, reset_singletons):
        """Empty AGENTIBRIDGE_REMOVE_TOOLS should leave all tools intact."""
        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()

        tools_before = [t.name for t in srv.mcp._tool_manager.list_tools()]

        with patch.dict("os.environ", {"AGENTIBRIDGE_REMOVE_TOOLS": ""}):
            import importlib
            import agentibridge.config

            importlib.reload(agentibridge.config)

            from agentibridge.config import AGENTIBRIDGE_REMOVE_TOOLS

            assert AGENTIBRIDGE_REMOVE_TOOLS == []

            tools_after = [t.name for t in srv.mcp._tool_manager.list_tools()]
            assert tools_before == tools_after


@pytest.mark.unit
class TestDispatchTask:
    def test_success(self, reset_singletons):
        import agentibridge.server as srv

        srv._store = MagicMock()
        srv._collector = _mock_collector()

        with patch("agentibridge.dispatch.dispatch_task", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = {
                "dispatched": True,
                "completed": True,
                "exit_code": 0,
                "duration_ms": 1000,
                "timed_out": False,
                "output": {"result": "done"},
                "error": None,
                "context_session": None,
                "prompt_length": 100,
            }

            result = json.loads(asyncio.run(srv.dispatch_task(task_description="Fix bug")))
            assert result["success"] is True
            assert result["dispatched"] is True
