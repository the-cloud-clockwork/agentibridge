"""Tests for agentibridge.config module."""

import pytest


@pytest.mark.unit
class TestEnvBool:
    def test_true_values(self, monkeypatch):
        from agentibridge.config import _env_bool

        for val in ("true", "True", "TRUE", "1", "yes", "Yes", "YES"):
            monkeypatch.setenv("TEST_BOOL", val)
            assert _env_bool("TEST_BOOL") is True, f"Expected True for {val!r}"

    def test_false_values(self, monkeypatch):
        from agentibridge.config import _env_bool

        for val in ("false", "False", "0", "no", "No", "anything", ""):
            monkeypatch.setenv("TEST_BOOL", val)
            assert _env_bool("TEST_BOOL") is False, f"Expected False for {val!r}"

    def test_default(self, monkeypatch):
        from agentibridge.config import _env_bool

        monkeypatch.delenv("TEST_NONEXISTENT", raising=False)
        assert _env_bool("TEST_NONEXISTENT", "true") is True
        assert _env_bool("TEST_NONEXISTENT", "false") is False


@pytest.mark.unit
class TestEnvInt:
    def test_default_value(self, monkeypatch):
        from agentibridge.config import _env_int

        monkeypatch.delenv("TEST_INT", raising=False)
        assert _env_int("TEST_INT", "42") == 42

    def test_env_override(self, monkeypatch):
        from agentibridge.config import _env_int

        monkeypatch.setenv("TEST_INT", "100")
        assert _env_int("TEST_INT", "42") == 100

    def test_min_val_clamp(self, monkeypatch):
        from agentibridge.config import _env_int

        monkeypatch.setenv("TEST_INT", "2")
        assert _env_int("TEST_INT", "10", min_val=5) == 5

    def test_max_val_clamp(self, monkeypatch):
        from agentibridge.config import _env_int

        monkeypatch.setenv("TEST_INT", "99999")
        assert _env_int("TEST_INT", "100", max_val=65535) == 65535

    def test_within_bounds(self, monkeypatch):
        from agentibridge.config import _env_int

        monkeypatch.setenv("TEST_INT", "8100")
        assert _env_int("TEST_INT", "80", min_val=1, max_val=65535) == 8100


@pytest.mark.unit
class TestConfigDefaults:
    def test_poll_interval_minimum(self, monkeypatch):
        """Poll interval should be clamped to minimum 5 seconds."""
        monkeypatch.setenv("AGENTIBRIDGE_POLL_INTERVAL", "1")
        # Need to reimport to pick up new env
        from agentibridge.config import _env_int

        val = _env_int("AGENTIBRIDGE_POLL_INTERVAL", "60", min_val=5)
        assert val == 5

    def test_port_bounds(self, monkeypatch):
        from agentibridge.config import _env_int

        monkeypatch.setenv("AGENTIBRIDGE_PORT", "0")
        val = _env_int("AGENTIBRIDGE_PORT", "8100", min_val=1, max_val=65535)
        assert val == 1

        monkeypatch.setenv("AGENTIBRIDGE_PORT", "70000")
        val = _env_int("AGENTIBRIDGE_PORT", "8100", min_val=1, max_val=65535)
        assert val == 65535

    def test_max_entries_allows_zero(self, monkeypatch):
        from agentibridge.config import _env_int

        monkeypatch.setenv("AGENTIBRIDGE_MAX_ENTRIES", "0")
        val = _env_int("AGENTIBRIDGE_MAX_ENTRIES", "500", min_val=0)
        assert val == 0


@pytest.mark.unit
class TestHostDefault:
    def test_default_host_is_localhost(self, monkeypatch):
        """Pin the localhost spelling — enterprise policies silently drop
        client entries whose url names 127.0.0.1, and AGENTIBRIDGE_HOST
        drives both the bind and any composed client url."""
        import importlib

        import agentibridge.config as config

        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
        monkeypatch.delenv("AGENTIBRIDGE_HOST", raising=False)
        importlib.reload(config)
        assert config.AGENTIBRIDGE_HOST == "localhost"
        monkeypatch.undo()
        importlib.reload(config)
