"""Tests for agentibridge.store module."""

from pathlib import Path
from unittest.mock import patch

import pytest

from agentibridge.store import SessionStore, _escape_redis_glob, _filepath_hash, _rkey

# Re-use conftest helpers
from tests.conftest import make_entry, make_meta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _force_redis(store: SessionStore, fake_redis):
    """Override a SessionStore instance so it always returns the fakeredis."""
    store._redis = fake_redis
    store._redis_checked = True


def _force_no_redis(store: SessionStore):
    """Override a SessionStore instance so it always returns None for Redis."""
    store._redis = None
    store._redis_checked = True


# ---------------------------------------------------------------------------
# Tests: module-level helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHelpers:
    """Tests for module-level helper functions."""

    def test_rkey_builds_namespaced_key(self):
        key = _rkey("session:abc:meta")
        assert key.endswith(":sb:session:abc:meta")
        assert "sb" in key

    def test_filepath_hash_deterministic(self):
        h1 = _filepath_hash("/tmp/foo.jsonl")
        h2 = _filepath_hash("/tmp/foo.jsonl")
        assert h1 == h2
        assert len(h1) == 12

    def test_filepath_hash_different_for_different_paths(self):
        h1 = _filepath_hash("/tmp/foo.jsonl")
        h2 = _filepath_hash("/tmp/bar.jsonl")
        assert h1 != h2

    def test_escape_redis_glob_escapes_star(self):
        assert _escape_redis_glob("foo*bar") == "foo\\*bar"

    def test_escape_redis_glob_escapes_question(self):
        assert _escape_redis_glob("foo?bar") == "foo\\?bar"

    def test_escape_redis_glob_escapes_brackets(self):
        assert _escape_redis_glob("foo[bar]") == "foo\\[bar\\]"

    def test_escape_redis_glob_escapes_all_special_chars(self):
        result = _escape_redis_glob("a*b?c[d]e")
        assert result == "a\\*b\\?c\\[d\\]e"

    def test_escape_redis_glob_no_special_chars(self):
        assert _escape_redis_glob("foobar") == "foobar"

    def test_escape_redis_glob_empty_string(self):
        assert _escape_redis_glob("") == ""


# ---------------------------------------------------------------------------
# Tests: Redis CRUD operations
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisUpsertAndGetMeta:
    """Test upsert_session and get_session_meta with Redis."""

    def test_upsert_and_get_meta_round_trip(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        meta = make_meta(session_id="s1", project_path="/home/user/dev/app")
        store.upsert_session(meta)

        retrieved = store.get_session_meta("s1")
        assert retrieved is not None
        assert retrieved.session_id == "s1"
        assert retrieved.project_path == "/home/user/dev/app"
        assert retrieved.num_user_turns == 5
        assert retrieved.num_assistant_turns == 5
        assert retrieved.num_tool_calls == 10
        assert retrieved.summary == "Test session summary"

    def test_upsert_updates_existing_meta(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        meta1 = make_meta(session_id="s1", summary="First")
        store.upsert_session(meta1)

        meta2 = make_meta(session_id="s1", summary="Updated summary")
        store.upsert_session(meta2)

        retrieved = store.get_session_meta("s1")
        assert retrieved.summary == "Updated summary"

    def test_get_meta_nonexistent_returns_none(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        result = store.get_session_meta("nonexistent")
        assert result is None

    def test_upsert_stores_boolean_and_int_fields(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        meta = make_meta(
            session_id="s1",
            has_subagents=True,
            file_size_bytes=12345,
            num_tool_calls=42,
        )
        store.upsert_session(meta)

        retrieved = store.get_session_meta("s1")
        assert retrieved.has_subagents is True
        assert retrieved.file_size_bytes == 12345
        assert retrieved.num_tool_calls == 42


@pytest.mark.unit
class TestRedisAddAndGetEntries:
    """Test add_entries and get_session_entries with Redis."""

    def test_add_and_get_entries_round_trip(self, mock_redis, sample_entries):
        store = SessionStore()
        _force_redis(store, mock_redis)

        store.add_entries("s1", sample_entries)
        result = store.get_session_entries("s1", offset=0, limit=50)

        assert len(result) == len(sample_entries)
        assert result[0].entry_type == "user"
        assert result[0].content == "Create a REST API"

    def test_add_entries_empty_list_is_noop(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        store.add_entries("s1", [])
        result = store.get_session_entries("s1")
        assert len(result) == 0

    def test_get_entries_with_offset_and_limit(self, mock_redis, sample_entries):
        store = SessionStore()
        _force_redis(store, mock_redis)

        store.add_entries("s1", sample_entries)

        # Get only entries 1-2 (offset=1, limit=2)
        result = store.get_session_entries("s1", offset=1, limit=2)
        assert len(result) == 2
        assert result[0].entry_type == "assistant"
        assert result[0].content == "Created CRUD endpoints"

    def test_get_entries_nonexistent_session_returns_empty(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        result = store.get_session_entries("nonexistent")
        assert result == []

    def test_add_entries_preserves_tool_names(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        entries = [
            make_entry("assistant", content="Did things", tool_names=["Write", "Edit"]),
        ]
        store.add_entries("s1", entries)

        result = store.get_session_entries("s1")
        assert result[0].tool_names == ["Write", "Edit"]

    def test_add_entries_truncates_content_to_500(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        long_content = "x" * 1000
        entries = [make_entry("user", content=long_content)]
        store.add_entries("s1", entries)

        result = store.get_session_entries("s1")
        # The store truncates content to 500 chars on add
        assert len(result[0].content) == 500

    def test_add_entries_appends_incrementally(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        batch1 = [make_entry("user", content="First", uuid="u1")]
        batch2 = [make_entry("user", content="Second", uuid="u2")]
        store.add_entries("s1", batch1)
        store.add_entries("s1", batch2)

        result = store.get_session_entries("s1", offset=0, limit=50)
        assert len(result) == 2
        assert result[0].content == "First"
        assert result[1].content == "Second"


@pytest.mark.unit
class TestRedisMaxEntriesTrimming:
    """Test that add_entries trims to _MAX_ENTRIES."""

    def test_entries_trimmed_to_max(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        # Patch _MAX_ENTRIES to a small value for testing
        with patch("agentibridge.store._MAX_ENTRIES", 5):
            entries = [make_entry("user", content=f"Message {i}", uuid=f"u{i}") for i in range(10)]
            store.add_entries("s1", entries)

        result = store.get_session_entries("s1", offset=0, limit=50)
        assert len(result) == 5
        # Should keep the last 5 entries (trimmed from the left)
        assert result[0].content == "Message 5"
        assert result[4].content == "Message 9"


# ---------------------------------------------------------------------------
# Tests: list_sessions with Redis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisListSessions:
    """Test list_sessions with Redis backend."""

    def _seed_sessions(self, store, mock_redis, count=5):
        """Helper: insert multiple sessions and return their IDs."""
        ids = []
        for i in range(count):
            sid = f"session-{i:03d}"
            meta = make_meta(
                session_id=sid,
                project_encoded=f"-home-user-dev-project{i % 2}",
                project_path=f"/home/user/dev/project{i % 2}",
                last_update=f"2025-06-01T{10 + i}:00:00Z",
            )
            store.upsert_session(meta)
            ids.append(sid)
        return ids

    def test_list_sessions_returns_all(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)
        self._seed_sessions(store, mock_redis, count=3)

        results = store.list_sessions(limit=20)
        assert len(results) == 3

    def test_list_sessions_limit(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)
        self._seed_sessions(store, mock_redis, count=5)

        results = store.list_sessions(limit=2)
        assert len(results) == 2

    def test_list_sessions_offset(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)
        self._seed_sessions(store, mock_redis, count=5)

        all_results = store.list_sessions(limit=20)
        offset_results = store.list_sessions(limit=20, offset=2)
        assert len(offset_results) == 3
        assert offset_results[0].session_id == all_results[2].session_id

    def test_list_sessions_sorted_by_recency(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)
        self._seed_sessions(store, mock_redis, count=3)

        results = store.list_sessions(limit=20)
        # Most recent first
        assert results[0].session_id == "session-002"
        assert results[1].session_id == "session-001"
        assert results[2].session_id == "session-000"

    def test_list_sessions_project_filter(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)
        self._seed_sessions(store, mock_redis, count=4)

        results = store.list_sessions(project="project0", limit=20)
        for r in results:
            assert "project0" in r.project_encoded

    def test_list_sessions_since_hours(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        # Insert one session with a recent timestamp
        recent = make_meta(
            session_id="recent",
            last_update="2099-01-01T00:00:00Z",  # Far future -> high score
        )
        store.upsert_session(recent)

        # Insert one session with an old timestamp
        old = make_meta(
            session_id="old",
            last_update="2020-01-01T00:00:00Z",  # Far past -> low score
        )
        store.upsert_session(old)

        # Filter for sessions in the last 1 hour
        results = store.list_sessions(since_hours=1, limit=20)
        # Only the "recent" session should match (its score is far in the future)
        session_ids = [r.session_id for r in results]
        assert "recent" in session_ids
        assert "old" not in session_ids

    def test_list_sessions_empty_store(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        results = store.list_sessions()
        assert results == []

    def test_upsert_adds_project_to_index_set(self, mock_redis):
        """Verify upsert_session adds the project_encoded to idx:projects SET."""
        store = SessionStore()
        _force_redis(store, mock_redis)

        meta = make_meta(
            session_id="s1",
            project_encoded="-home-user-dev-myapp",
        )
        store.upsert_session(meta)

        projects = mock_redis.smembers(_rkey("idx:projects"))
        assert "-home-user-dev-myapp" in projects

    def test_list_sessions_uses_project_index_set(self, mock_redis):
        """Verify list_sessions with project filter uses SMEMBERS, not SCAN."""
        store = SessionStore()
        _force_redis(store, mock_redis)

        # Seed two projects
        for i, proj in enumerate(["myapp", "backend"]):
            meta = make_meta(
                session_id=f"s{i}",
                project_encoded=f"-home-user-dev-{proj}",
                project_path=f"/home/user/dev/{proj}",
                last_update=f"2025-06-01T{10 + i}:00:00Z",
            )
            store.upsert_session(meta)

        # Verify project index SET has both projects
        projects = mock_redis.smembers(_rkey("idx:projects"))
        assert len(projects) == 2

        # Filter by project
        results = store.list_sessions(project="myapp", limit=20)
        assert len(results) == 1
        assert results[0].project_encoded == "-home-user-dev-myapp"


# ---------------------------------------------------------------------------
# Tests: search_sessions with Redis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisSearchSessions:
    """Test search_sessions with Redis backend."""

    def test_search_finds_matching_content(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        meta = make_meta(session_id="s1")
        store.upsert_session(meta)
        entries = [
            make_entry("user", content="Deploy the Docker containers"),
            make_entry("assistant", content="Containers deployed successfully"),
        ]
        store.add_entries("s1", entries)

        results = store.search_sessions("Docker")
        assert len(results) == 1
        assert results[0]["session_id"] == "s1"
        assert "Docker" in results[0]["content_preview"]

    def test_search_case_insensitive(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        meta = make_meta(session_id="s1")
        store.upsert_session(meta)
        store.add_entries("s1", [make_entry("user", content="Docker setup")])

        results = store.search_sessions("docker")
        assert len(results) == 1

    def test_search_no_match_returns_empty(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        meta = make_meta(session_id="s1")
        store.upsert_session(meta)
        store.add_entries("s1", [make_entry("user", content="Hello world")])

        results = store.search_sessions("kubernetes")
        assert results == []

    def test_search_with_project_filter(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        # Two sessions, different projects, same keyword
        meta1 = make_meta(
            session_id="s1",
            project_encoded="-home-user-dev-frontend",
            project_path="/home/user/dev/frontend",
        )
        meta2 = make_meta(
            session_id="s2",
            project_encoded="-home-user-dev-backend",
            project_path="/home/user/dev/backend",
        )
        store.upsert_session(meta1)
        store.upsert_session(meta2)
        store.add_entries("s1", [make_entry("user", content="Fix the bug")])
        store.add_entries("s2", [make_entry("user", content="Fix the bug")])

        results = store.search_sessions("bug", project="frontend")
        assert len(results) == 1
        assert results[0]["session_id"] == "s1"

    def test_search_respects_limit(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        for i in range(5):
            sid = f"s{i}"
            store.upsert_session(make_meta(session_id=sid))
            store.add_entries(sid, [make_entry("user", content="common keyword")])

        results = store.search_sessions("common", limit=2)
        assert len(results) == 2

    def test_search_one_match_per_session(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        meta = make_meta(session_id="s1")
        store.upsert_session(meta)
        entries = [
            make_entry("user", content="test keyword here"),
            make_entry("assistant", content="test keyword again"),
        ]
        store.add_entries("s1", entries)

        results = store.search_sessions("keyword")
        # Only one result per session (breaks after first match)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Tests: count_entries with Redis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisCountEntries:
    """Test count_entries with Redis backend."""

    def test_count_entries_returns_correct_count(self, mock_redis, sample_entries):
        store = SessionStore()
        _force_redis(store, mock_redis)

        store.add_entries("s1", sample_entries)
        assert store.count_entries("s1") == len(sample_entries)

    def test_count_entries_nonexistent_session(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        assert store.count_entries("nonexistent") == 0

    def test_count_entries_empty_session(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        # Upsert meta but no entries
        store.upsert_session(make_meta(session_id="s1"))
        assert store.count_entries("s1") == 0


# ---------------------------------------------------------------------------
# Tests: file position tracking with Redis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisPositionTracking:
    """Test get_file_position and save_file_position with Redis."""

    def test_save_and_get_position(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        store.save_file_position("/tmp/test.jsonl", 4096)
        pos = store.get_file_position("/tmp/test.jsonl")
        assert pos == 4096

    def test_get_position_default_zero(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        pos = store.get_file_position("/tmp/never_saved.jsonl")
        assert pos == 0

    def test_position_is_filepath_specific(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        store.save_file_position("/tmp/a.jsonl", 100)
        store.save_file_position("/tmp/b.jsonl", 200)

        assert store.get_file_position("/tmp/a.jsonl") == 100
        assert store.get_file_position("/tmp/b.jsonl") == 200

    def test_position_overwrites(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        store.save_file_position("/tmp/x.jsonl", 50)
        store.save_file_position("/tmp/x.jsonl", 999)
        assert store.get_file_position("/tmp/x.jsonl") == 999


# ---------------------------------------------------------------------------
# Tests: filesystem fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFilesystemFallbackGetMeta:
    """Test get_session_meta falling back to filesystem."""

    def test_get_meta_from_filesystem(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        meta = store.get_session_meta("session-001")
        assert meta is not None
        assert meta.session_id == "session-001"
        assert meta.project_path == "/home/user/dev/myapp"
        assert meta.cwd == "/home/user/dev/myapp"
        assert meta.git_branch == "main"

    def test_get_meta_nonexistent_from_filesystem(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        meta = store.get_session_meta("nonexistent-session")
        assert meta is None


@pytest.mark.unit
class TestFilesystemFallbackGetEntries:
    """Test get_session_entries falling back to filesystem."""

    def test_get_entries_from_filesystem(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        entries = store.get_session_entries("session-001")
        assert len(entries) > 0
        # First entry should be user "Help me create a Docker Compose setup"
        assert entries[0].entry_type == "user"
        assert "Docker Compose" in entries[0].content

    def test_get_entries_with_offset_limit(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        all_entries = store.get_session_entries("session-001", offset=0, limit=1000)
        subset = store.get_session_entries("session-001", offset=1, limit=2)

        assert len(subset) == 2
        assert subset[0].entry_type == all_entries[1].entry_type
        assert subset[0].content == all_entries[1].content

    def test_get_entries_nonexistent_session(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        entries = store.get_session_entries("nonexistent")
        assert entries == []


@pytest.mark.unit
class TestFilesystemFallbackListSessions:
    """Test list_sessions falling back to filesystem."""

    def test_list_sessions_from_filesystem(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        results = store.list_sessions(limit=20)
        assert len(results) == 2
        session_ids = {r.session_id for r in results}
        assert "session-001" in session_ids
        assert "session-002" in session_ids

    def test_list_sessions_with_project_filter(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        results = store.list_sessions(project="myapp", limit=20)
        assert len(results) == 1
        assert results[0].session_id == "session-001"

    def test_list_sessions_project_filter_by_decoded_path(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        # The decoded path for "-home-user-dev-backend" is "/home/user/dev/backend"
        results = store.list_sessions(project="backend", limit=20)
        assert len(results) == 1
        assert results[0].session_id == "session-002"

    def test_list_sessions_limit_and_offset(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        all_results = store.list_sessions(limit=20)
        first_only = store.list_sessions(limit=1, offset=0)
        second_only = store.list_sessions(limit=1, offset=1)

        assert len(first_only) == 1
        assert len(second_only) == 1
        assert first_only[0].session_id == all_results[0].session_id
        assert second_only[0].session_id == all_results[1].session_id

    def test_list_sessions_empty_dir(self, tmp_path):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = tmp_path

        results = store.list_sessions()
        assert results == []

    def test_list_sessions_since_hours_filter(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        # The fixture files have been just created, so mtime is very recent.
        # since_hours=1 should return them.
        results = store.list_sessions(since_hours=1, limit=20)
        assert len(results) == 2

        # since_hours filter with 0 means no filter
        results_no_filter = store.list_sessions(since_hours=0, limit=20)
        assert len(results_no_filter) == 2


@pytest.mark.unit
class TestFilesystemFallbackSearch:
    """Test search_sessions falling back to filesystem."""

    def test_search_finds_keyword(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        results = store.search_sessions("Docker Compose")
        assert len(results) >= 1
        assert any("Docker" in r["content_preview"] for r in results)

    def test_search_case_insensitive(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        results = store.search_sessions("docker compose")
        assert len(results) >= 1

    def test_search_no_match(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        results = store.search_sessions("xyznonsensequery")
        assert results == []

    def test_search_with_project_filter(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        # "Migrate database" is only in session-002 (backend project)
        results = store.search_sessions("Migrate database", project="backend")
        assert len(results) == 1
        assert results[0]["session_id"] == "session-002"

    def test_search_project_filter_excludes_other_projects(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        # "Docker Compose" is in session-001 (myapp project), not backend
        results = store.search_sessions("Docker Compose", project="backend")
        assert results == []

    def test_search_respects_limit(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        # Both sessions have content, so a broad query could match multiple
        results = store.search_sessions("e", limit=1)
        assert len(results) <= 1

    def test_search_result_structure(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        results = store.search_sessions("Docker")
        assert len(results) >= 1
        result = results[0]
        assert "session_id" in result
        assert "project_path" in result
        assert "entry_type" in result
        assert "content_preview" in result
        assert "timestamp" in result


# ---------------------------------------------------------------------------
# Tests: filesystem fallback — count_entries
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFilesystemFallbackCountEntries:
    """Test count_entries falling back to filesystem."""

    def test_count_entries_from_filesystem(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        count = store.count_entries("session-001")
        # The sample_transcript.jsonl has multiple user/assistant/summary/system entries
        # but skips progress, queue-operation, file-history-snapshot
        assert count > 0

    def test_count_entries_nonexistent_session(self, temp_projects_dir):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = temp_projects_dir

        assert store.count_entries("nonexistent") == 0


# ---------------------------------------------------------------------------
# Tests: file-based position tracking (no Redis)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFilePositionTracking:
    """Test file-based position tracking when Redis is unavailable."""

    def test_save_and_get_position_via_file(self, tmp_path):
        store = SessionStore()
        _force_no_redis(store)
        store._POS_DIR = tmp_path / "positions"

        store.save_file_position("/tmp/test.jsonl", 2048)
        pos = store.get_file_position("/tmp/test.jsonl")
        assert pos == 2048

    def test_get_position_default_zero_when_no_file(self, tmp_path):
        store = SessionStore()
        _force_no_redis(store)
        store._POS_DIR = tmp_path / "positions"

        pos = store.get_file_position("/tmp/unseen.jsonl")
        assert pos == 0

    def test_position_directory_created_on_save(self, tmp_path):
        store = SessionStore()
        _force_no_redis(store)
        pos_dir = tmp_path / "new_positions"
        store._POS_DIR = pos_dir

        assert not pos_dir.exists()
        store.save_file_position("/tmp/test.jsonl", 100)
        assert pos_dir.exists()

    def test_position_overwrites_via_file(self, tmp_path):
        store = SessionStore()
        _force_no_redis(store)
        store._POS_DIR = tmp_path / "positions"

        store.save_file_position("/tmp/test.jsonl", 100)
        store.save_file_position("/tmp/test.jsonl", 500)
        assert store.get_file_position("/tmp/test.jsonl") == 500

    def test_position_different_files(self, tmp_path):
        store = SessionStore()
        _force_no_redis(store)
        store._POS_DIR = tmp_path / "positions"

        store.save_file_position("/tmp/a.jsonl", 111)
        store.save_file_position("/tmp/b.jsonl", 222)

        assert store.get_file_position("/tmp/a.jsonl") == 111
        assert store.get_file_position("/tmp/b.jsonl") == 222


# ---------------------------------------------------------------------------
# Tests: SessionStore initialisation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSessionStoreInit:
    """Test SessionStore constructor behavior."""

    def test_default_projects_dir(self):
        store = SessionStore()
        expected = Path.home() / ".claude" / "projects"
        assert store._projects_dir == expected

    def test_custom_home_dir_via_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_CODE_HOME_DIR", str(tmp_path))
        store = SessionStore()
        assert store._projects_dir == tmp_path / "projects"
        assert store._history_file == tmp_path / "history.jsonl"

    def test_lazy_redis_not_checked_on_init(self):
        store = SessionStore()
        assert store._redis is None
        assert store._redis_checked is False


# ---------------------------------------------------------------------------
# Tests: Redis lazy connection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisLazyConnection:
    """Test that _get_redis is lazy and fail-safe."""

    def test_get_redis_returns_none_on_import_error(self):
        store = SessionStore()

        with patch("agentibridge.store.SessionStore._get_redis", return_value=None):
            result = store._get_redis()
            assert result is None

    def test_get_redis_caches_after_first_call(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        r1 = store._get_redis()
        r2 = store._get_redis()
        assert r1 is r2
        assert r1 is mock_redis


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_upsert_session_no_redis_is_noop(self):
        store = SessionStore()
        _force_no_redis(store)

        meta = make_meta(session_id="s1")
        # Should not raise
        store.upsert_session(meta)

    def test_add_entries_no_redis_is_noop(self):
        store = SessionStore()
        _force_no_redis(store)

        entries = [make_entry("user", content="test")]
        # Should not raise
        store.add_entries("s1", entries)

    def test_get_entries_offset_beyond_length(self, mock_redis, sample_entries):
        store = SessionStore()
        _force_redis(store, mock_redis)

        store.add_entries("s1", sample_entries)
        result = store.get_session_entries("s1", offset=999, limit=50)
        assert result == []

    def test_count_entries_after_incremental_adds(self, mock_redis):
        store = SessionStore()
        _force_redis(store, mock_redis)

        store.add_entries("s1", [make_entry("user", content="A", uuid="u1")])
        assert store.count_entries("s1") == 1

        store.add_entries(
            "s1",
            [
                make_entry("user", content="B", uuid="u2"),
                make_entry("user", content="C", uuid="u3"),
            ],
        )
        assert store.count_entries("s1") == 3

    def test_list_sessions_nonexistent_projects_dir(self):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = Path("/nonexistent/path")

        results = store.list_sessions()
        assert results == []

    def test_search_sessions_nonexistent_projects_dir(self):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = Path("/nonexistent/path")

        results = store.search_sessions("anything")
        assert results == []

    def test_count_entries_nonexistent_projects_dir(self):
        store = SessionStore()
        _force_no_redis(store)
        store._projects_dir = Path("/nonexistent/path")

        count = store.count_entries("anything")
        assert count == 0
