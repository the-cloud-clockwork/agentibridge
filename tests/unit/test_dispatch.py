"""Tests for agentibridge.dispatch module."""

from unittest.mock import MagicMock, patch

import pytest

from agentibridge.dispatch import restore_session_context
from tests.conftest import make_entry, make_meta


@pytest.mark.unit
class TestRestoreSessionContext:
    def test_formats_context(self):
        meta = make_meta(
            session_id="s1",
            project_path="/home/user/dev/app",
            git_branch="main",
            summary="Built REST API",
        )
        entries = [
            make_entry("user", content="Create API"),
            make_entry("assistant", content="Created endpoints", tool_names=["Write"]),
        ]

        mock_store = MagicMock()
        mock_store.get_session_meta.return_value = meta
        mock_store.get_session_entries.return_value = entries

        with patch("agentibridge.store.SessionStore", return_value=mock_store):
            context = restore_session_context("s1", last_n=20)

        assert "RESTORED SESSION CONTEXT" in context
        assert "END OF RESTORED CONTEXT" in context
        assert "/home/user/dev/app" in context
        assert "main" in context
        assert "[USER]" in context
        assert "[ASSISTANT]" in context
        assert "Create API" in context
        assert "Created endpoints" in context
        assert "(tools: Write)" in context

    def test_missing_session_raises(self):
        mock_store = MagicMock()
        mock_store.get_session_meta.return_value = None

        with patch("agentibridge.store.SessionStore", return_value=mock_store):
            with pytest.raises(ValueError, match="Session not found"):
                restore_session_context("nonexistent")

    def test_last_n_limits_entries(self):
        meta = make_meta()
        entries = [make_entry("user", content=f"Turn {i}") for i in range(20)]

        mock_store = MagicMock()
        mock_store.get_session_meta.return_value = meta
        mock_store.get_session_entries.return_value = entries

        with patch("agentibridge.store.SessionStore", return_value=mock_store):
            context = restore_session_context("s1", last_n=5)

        # Should contain turns 15-19 but not 0-14
        assert "Turn 19" in context
        assert "Turn 15" in context
        assert "Turn 0" not in context

    def test_summary_in_entries(self):
        meta = make_meta()
        entries = [
            make_entry("summary", content="Session summary here"),
        ]

        mock_store = MagicMock()
        mock_store.get_session_meta.return_value = meta
        mock_store.get_session_entries.return_value = entries

        with patch("agentibridge.store.SessionStore", return_value=mock_store):
            context = restore_session_context("s1")

        assert "[SUMMARY]" in context
        assert "Session summary here" in context
