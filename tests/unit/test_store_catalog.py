"""Tests for Phase 5 store methods (memory, history)."""

import pytest

from agentibridge.store import SessionStore
from tests.conftest import make_history_entry, make_memory_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _force_redis(store: SessionStore, fake_redis):
    store._redis = fake_redis
    store._redis_checked = True


def _force_no_redis(store: SessionStore):
    store._redis = None
    store._redis_checked = True


# ---------------------------------------------------------------------------
# Tests: Memory — Redis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisMemory:
    def test_upsert_and_list_memory(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        mem = make_memory_file()
        store.upsert_memory_file(mem)

        results = store.list_memory_files()
        assert len(results) == 1
        assert results[0].filename == "MEMORY.md"
        assert results[0].project_path == "/home/user/dev/myapp"

    def test_upsert_and_get_memory(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        mem = make_memory_file()
        store.upsert_memory_file(mem)

        result = store.get_memory_file("-home-user-dev-myapp", "MEMORY.md")
        assert result is not None
        assert result.content == "# Project Memory\n\nKey decisions go here."

    def test_get_memory_nonexistent(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        result = store.get_memory_file("-nonexistent", "MEMORY.md")
        assert result is None

    def test_list_memory_with_project_filter(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        mem1 = make_memory_file(project_encoded="-home-user-dev-frontend", project_path="/home/user/dev/frontend")
        mem2 = make_memory_file(project_encoded="-home-user-dev-backend", project_path="/home/user/dev/backend")
        store.upsert_memory_file(mem1)
        store.upsert_memory_file(mem2)

        results = store.list_memory_files(project="frontend")
        assert len(results) == 1
        assert "frontend" in results[0].project_encoded

    def test_list_memory_empty(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        results = store.list_memory_files()
        assert results == []

    def test_upsert_memory_updates_existing(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        mem1 = make_memory_file(content="Version 1")
        store.upsert_memory_file(mem1)

        mem2 = make_memory_file(content="Version 2")
        store.upsert_memory_file(mem2)

        result = store.get_memory_file("-home-user-dev-myapp", "MEMORY.md")
        assert result.content == "Version 2"


# ---------------------------------------------------------------------------
# Tests: Memory — File fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFileMemory:
    def test_list_memory_from_filesystem(self, temp_memory_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_memory_dir

        results = store.list_memory_files()
        assert len(results) == 3

    def test_list_memory_with_project_filter(self, temp_memory_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_memory_dir

        results = store.list_memory_files(project="myapp")
        assert len(results) == 2  # MEMORY.md + patterns.md

    def test_get_memory_from_filesystem(self, temp_memory_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_memory_dir

        result = store.get_memory_file("-home-user-dev-myapp", "MEMORY.md")
        assert result is not None
        assert "Key patterns here" in result.content

    def test_get_memory_nonexistent(self, temp_memory_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_memory_dir

        result = store.get_memory_file("-home-user-dev-myapp", "nonexistent.md")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: Plans — Redis
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: History — Redis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisHistory:
    def test_add_and_search_history(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        entries = [
            make_history_entry(display="Deploy Docker containers", session_id="s1"),
            make_history_entry(display="Fix authentication bug", session_id="s2"),
            make_history_entry(display="Docker compose setup", session_id="s3"),
        ]
        store.add_history_entries(entries)

        results, total = store.search_history(query="Docker")
        assert total == 2
        assert len(results) == 2

    def test_search_history_with_project_filter(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        entries = [
            make_history_entry(display="Fix bug", project="/home/user/dev/frontend"),
            make_history_entry(display="Fix bug", project="/home/user/dev/backend"),
        ]
        store.add_history_entries(entries)

        results, total = store.search_history(query="bug", project="frontend")
        assert total == 1

    def test_search_history_with_session_filter(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        entries = [
            make_history_entry(display="Task A", session_id="s1"),
            make_history_entry(display="Task B", session_id="s2"),
        ]
        store.add_history_entries(entries)

        results, total = store.search_history(session_id="s1")
        assert total == 1
        assert results[0].session_id == "s1"

    def test_search_history_empty(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        results, total = store.search_history(query="anything")
        assert total == 0
        assert results == []

    def test_search_history_limit_offset(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        entries = [make_history_entry(display=f"Entry {i}", session_id=f"s{i}") for i in range(10)]
        store.add_history_entries(entries)

        results, total = store.search_history(limit=3, offset=2)
        assert total == 10
        assert len(results) == 3

    def test_add_history_empty_list(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        store.add_history_entries([])
        results, total = store.search_history()
        assert total == 0


# ---------------------------------------------------------------------------
# Tests: History — File fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFileHistory:
    def test_search_history_from_file(self, temp_history_file):
        store = SessionStore()
        _force_no_redis(store)
        store._history_file = temp_history_file

        results, total = store.search_history(query="Docker")
        assert total == 1
        assert "Docker" in results[0].display

    def test_search_history_with_project_filter_from_file(self, temp_history_file):
        store = SessionStore()
        _force_no_redis(store)
        store._history_file = temp_history_file

        results, total = store.search_history(project="backend")
        assert total == 2  # "Migrate database" + "Fix auth bug"

    def test_search_history_all_entries(self, temp_history_file):
        store = SessionStore()
        _force_no_redis(store)
        store._history_file = temp_history_file

        results, total = store.search_history()
        assert total == 5


# ---------------------------------------------------------------------------
# Tests: Codename index — Redis
# ---------------------------------------------------------------------------
