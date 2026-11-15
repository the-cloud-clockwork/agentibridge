"""Tests for agentibridge.catalog module."""

import json
from pathlib import Path

import pytest

from agentibridge.catalog import (
    HistoryEntry,
    MemoryFile,
    parse_history,
    scan_memory_files,
)
from tests.conftest import make_history_entry, make_memory_file


# ---------------------------------------------------------------------------
# Tests: MemoryFile dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMemoryFile:
    def test_to_dict_from_dict_roundtrip(self):
        mem = make_memory_file()
        d = mem.to_dict()
        restored = MemoryFile.from_dict(d)
        assert restored == mem

    def test_from_dict_coerces_file_size_string(self):
        d = make_memory_file().to_dict()
        d["file_size_bytes"] = "256"
        restored = MemoryFile.from_dict(d)
        assert restored.file_size_bytes == 256

    def test_from_dict_ignores_extra_keys(self):
        d = make_memory_file().to_dict()
        d["extra_key"] = "ignored"
        restored = MemoryFile.from_dict(d)
        assert restored.filename == "MEMORY.md"


# ---------------------------------------------------------------------------
# Tests: HistoryEntry dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHistoryEntry:
    def test_to_dict_from_dict_roundtrip(self):
        entry = make_history_entry()
        d = entry.to_dict()
        restored = HistoryEntry.from_dict(d)
        assert restored == entry

    def test_from_dict_ignores_extra_keys(self):
        d = make_history_entry().to_dict()
        d["extra"] = "value"
        restored = HistoryEntry.from_dict(d)
        assert restored.display == "Help me create a Docker Compose setup"


# ---------------------------------------------------------------------------
# Tests: scan_memory_files
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScanMemoryFiles:
    def test_scans_fixture_dir(self, temp_memory_dir):
        results = scan_memory_files(temp_memory_dir)
        assert len(results) == 3  # 2 from myapp, 1 from backend

        filenames = {r.filename for r in results}
        assert "MEMORY.md" in filenames
        assert "patterns.md" in filenames

    def test_content_loaded(self, temp_memory_dir):
        results = scan_memory_files(temp_memory_dir)
        myapp_memory = [r for r in results if r.filename == "MEMORY.md" and "myapp" in r.project_encoded]
        assert len(myapp_memory) == 1
        assert "Key patterns here" in myapp_memory[0].content

    def test_project_path_decoded(self, temp_memory_dir):
        results = scan_memory_files(temp_memory_dir)
        paths = {r.project_path for r in results}
        assert "/home/user/dev/myapp" in paths
        assert "/home/user/dev/backend" in paths

    def test_empty_dir(self, tmp_path):
        results = scan_memory_files(tmp_path)
        assert results == []

    def test_nonexistent_dir(self):
        results = scan_memory_files(Path("/nonexistent/dir"))
        assert results == []

    def test_content_truncated(self, tmp_path):
        proj = tmp_path / "-test-project" / "memory"
        proj.mkdir(parents=True)
        (proj / "BIG.md").write_text("x" * 200)

        results = scan_memory_files(tmp_path, max_content=50)
        assert len(results) == 1
        assert len(results[0].content) == 50

    def test_has_timestamps(self, temp_memory_dir):
        results = scan_memory_files(temp_memory_dir)
        for r in results:
            assert r.last_modified  # non-empty ISO timestamp
            assert "T" in r.last_modified


# ---------------------------------------------------------------------------
# Tests: parse_history
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseHistory:
    def test_parses_all_entries(self, temp_history_file):
        entries, offset = parse_history(temp_history_file)
        assert len(entries) == 5
        assert entries[0].display == "Help me create a Docker Compose setup"
        assert entries[0].session_id == "session-001"
        assert entries[0].project == "/home/user/dev/myapp"

    def test_timestamps_converted_to_iso(self, temp_history_file):
        entries, _ = parse_history(temp_history_file)
        for e in entries:
            assert e.timestamp  # non-empty
            assert "T" in e.timestamp  # ISO format

    def test_returns_byte_offset(self, temp_history_file):
        entries, offset = parse_history(temp_history_file)
        assert offset > 0
        assert offset == temp_history_file.stat().st_size

    def test_incremental_parsing(self, temp_history_file):
        # Read all first
        entries1, offset1 = parse_history(temp_history_file)
        assert len(entries1) == 5

        # No new data — should return empty
        entries2, offset2 = parse_history(temp_history_file, offset=offset1)
        assert len(entries2) == 0
        assert offset2 == offset1

        # Append more entries
        with open(temp_history_file, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "display": "New entry after offset",
                        "timestamp": 1717240000000,
                        "projectPath": "/home/user/dev/new",
                        "sessionId": "session-099",
                    }
                )
                + "\n"
            )

        # Resume from offset — should get only the new entry
        entries3, offset3 = parse_history(temp_history_file, offset=offset1)
        assert len(entries3) == 1
        assert entries3[0].display == "New entry after offset"
        assert offset3 > offset1

    def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        entries, offset = parse_history(empty)
        assert entries == []

    def test_nonexistent_file(self):
        entries, offset = parse_history(Path("/nonexistent/history.jsonl"))
        assert entries == []
        assert offset == 0

    def test_skips_entries_without_display(self, tmp_path):
        f = tmp_path / "history.jsonl"
        f.write_text(json.dumps({"timestamp": 1717236000000, "projectPath": "/p", "sessionId": "s"}) + "\n")
        entries, _ = parse_history(f)
        assert len(entries) == 0

    def test_handles_malformed_lines(self, tmp_path):
        f = tmp_path / "history.jsonl"
        f.write_text(
            "not json\n"
            + json.dumps({"display": "valid", "timestamp": 1717236000000, "projectPath": "/p", "sessionId": "s"})
            + "\n"
        )
        entries, _ = parse_history(f)
        assert len(entries) == 1
        assert entries[0].display == "valid"
