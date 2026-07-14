"""Unit tests for the A2A registry — transport field + local agent integration.

Focused on the Phase 6 additions (transport discriminator, computed local-agent
merge into read paths, and the local dispatch branch). Uses the file-fallback
path (get_redis patched to None) + a tmp _AGENTS_DIR, mirroring test_dispatch.py.
"""

import asyncio

import pytest

import agentibridge.registry as registry_mod
from agentibridge.registry import get_agent, list_agents, register_agent, route_to_agent


@pytest.fixture
def temp_agents_dir(tmp_path):
    original = registry_mod._AGENTS_DIR
    registry_mod._AGENTS_DIR = tmp_path / "agents"
    try:
        yield registry_mod._AGENTS_DIR
    finally:
        registry_mod._AGENTS_DIR = original


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr("agentibridge.registry.get_redis", lambda: None)


@pytest.fixture
def no_local(monkeypatch):
    # Neutralize the computed local-agent merge unless a test opts in.
    monkeypatch.setattr("agentibridge.local_agents.discover_local_agents", lambda *a, **k: [])
    monkeypatch.setattr("agentibridge.local_agents.get_local_agent", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _reset_hub_cache():
    import agentibridge.local_agents as la

    la.reset_caches()
    yield
    la.reset_caches()


@pytest.fixture
def enable_local(monkeypatch):
    monkeypatch.setattr("agentibridge.local_agents.local_agents_enabled", lambda: True)


def _local_rec(agent_id, package_path, status="online"):
    return {
        "agent_id": agent_id,
        "agent_name": agent_id,
        "agent_type": "local",
        "capabilities": ["local", f"agent:{agent_id}"],
        "endpoint": "",
        "transport": "local",
        "status": status,
        "metadata": {"package_path": package_path, "available_capacity": 1 if status == "online" else 0},
        "effective_status": status,
    }


@pytest.mark.unit
class TestTransportField:
    def test_default_transport_http(self, temp_agents_dir, no_redis, no_local):
        register_agent("svc-1", endpoint="http://x:8200")
        assert get_agent("svc-1")["transport"] == "http"

    def test_register_local_transport(self, temp_agents_dir, no_redis, no_local):
        register_agent("loc-1", transport="local", metadata={"package_path": "/tmp/pkg"})
        agent = get_agent("loc-1")
        assert agent["transport"] == "local"
        assert agent["metadata"]["package_path"] == "/tmp/pkg"


@pytest.mark.unit
class TestListAgentsMerge:
    def test_merges_local_agents(self, temp_agents_dir, no_redis, monkeypatch):
        register_agent("svc-1", agent_type="executor", endpoint="http://x")
        monkeypatch.setattr(
            "agentibridge.local_agents.discover_local_agents",
            lambda *a, **k: [_local_rec("coding-agent", "/tmp/pkg")],
        )
        monkeypatch.setattr("agentibridge.local_agents.filter_records", lambda recs, *a, **k: recs)
        ids = {a["agent_id"] for a in list_agents()}
        assert "svc-1" in ids
        assert "coding-agent" in ids

    def test_dedup_registered_wins(self, temp_agents_dir, no_redis, monkeypatch):
        register_agent("dup", endpoint="http://x", agent_type="executor")
        monkeypatch.setattr(
            "agentibridge.local_agents.discover_local_agents",
            lambda *a, **k: [_local_rec("dup", "/tmp/pkg")],
        )
        monkeypatch.setattr("agentibridge.local_agents.filter_records", lambda recs, *a, **k: recs)
        matches = [a for a in list_agents() if a["agent_id"] == "dup"]
        assert len(matches) == 1
        assert matches[0]["agent_type"] == "executor"  # registered record wins

    def test_local_not_starved_when_registered_fills_limit(self, temp_agents_dir, no_redis, monkeypatch):
        # limit already filled by registered agents must not silently drop local.
        register_agent("svc-1", agent_type="executor", endpoint="http://x")
        monkeypatch.setattr(
            "agentibridge.local_agents.discover_local_agents",
            lambda *a, **k: [_local_rec("coding-agent", "/tmp/pkg")],
        )
        monkeypatch.setattr("agentibridge.local_agents.filter_records", lambda recs, *a, **k: recs)
        ids = {a["agent_id"] for a in list_agents(limit=1)}
        assert "coding-agent" in ids


@pytest.mark.unit
class TestGetAgentLocalFallback:
    def test_local_fallback_hit(self, temp_agents_dir, no_redis, monkeypatch):
        rec = _local_rec("ghost", "/tmp/pkg")
        monkeypatch.setattr(
            "agentibridge.local_agents.get_local_agent",
            lambda agent_id, *a, **k: rec if agent_id == "ghost" else None,
        )
        assert get_agent("ghost")["transport"] == "local"

    def test_local_fallback_miss(self, temp_agents_dir, no_redis, no_local):
        assert get_agent("absent") is None


@pytest.mark.unit
class TestRouteToLocalAgent:
    def test_local_dispatch_nowait(self, temp_agents_dir, no_redis, tmp_path, enable_local, monkeypatch):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        monkeypatch.setattr(
            "agentibridge.local_agents.get_local_agent",
            lambda agent_id, *a, **k: _local_rec("loc", str(pkg)) if agent_id == "loc" else None,
        )

        calls = {}

        async def fake_dispatch(task_description="", project="", **k):
            calls["task"] = task_description
            calls["project"] = project
            return {"dispatched": True, "job_id": "job-1", "status": "running"}

        monkeypatch.setattr("agentibridge.dispatch.dispatch_task", fake_dispatch)

        result = asyncio.run(route_to_agent("loc", "do the thing"))
        assert result["success"] is True
        assert result["transport"] == "local"
        assert result["job_id"] == "job-1"
        assert calls["project"] == str(pkg)
        assert calls["task"] == "do the thing"

    def test_cold_start_when_offline(self, temp_agents_dir, no_redis, tmp_path, enable_local, monkeypatch):
        # Direct dispatch must cold-start even when no live session (offline).
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        monkeypatch.setattr(
            "agentibridge.local_agents.get_local_agent",
            lambda agent_id, *a, **k: _local_rec("loc", str(pkg), status="offline"),
        )

        async def fake_dispatch(**k):
            return {"dispatched": True, "job_id": "j", "status": "running"}

        monkeypatch.setattr("agentibridge.dispatch.dispatch_task", fake_dispatch)
        result = asyncio.run(route_to_agent("loc", "task"))
        assert result["success"] is True  # not blocked by offline effective_status

    def test_local_dispatch_wait(self, temp_agents_dir, no_redis, tmp_path, enable_local, monkeypatch):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        monkeypatch.setattr(
            "agentibridge.local_agents.get_local_agent",
            lambda agent_id, *a, **k: _local_rec("loc", str(pkg)),
        )

        from agentibridge.claude_runner import ClaudeResult

        async def fake_run(prompt="", cwd="", timeout=None, **k):
            assert cwd == str(pkg)
            return ClaudeResult(success=True, result="done", session_id="cs-1")

        monkeypatch.setattr("agentibridge.claude_runner.run_claude", fake_run)

        result = asyncio.run(route_to_agent("loc", "task", wait=True))
        assert result["success"] is True
        assert result["result"] == "done"
        assert result["session_id"] == "cs-1"

    def test_missing_package_path(self, temp_agents_dir, no_redis, enable_local, monkeypatch):
        bad = {"agent_id": "loc", "transport": "local", "metadata": {}, "effective_status": "online"}
        monkeypatch.setattr("agentibridge.local_agents.get_local_agent", lambda agent_id, *a, **k: bad)
        result = asyncio.run(route_to_agent("loc", "task"))
        assert result["success"] is False
        assert "package_path" in result["error"]

    def test_disabled_persisted_local_card_rejected(self, temp_agents_dir, no_redis, tmp_path, monkeypatch):
        # A persisted transport=local card must NOT dispatch while the feature is
        # off — even though get_agent finds the persisted record.
        evil = tmp_path / "evil"
        evil.mkdir()
        register_agent("x", transport="local", metadata={"package_path": str(evil)})
        monkeypatch.setattr("agentibridge.local_agents.local_agents_enabled", lambda: False)
        result = asyncio.run(route_to_agent("x", "task"))
        assert result["success"] is False
        assert "disabled" in result["error"]

    def test_forged_local_card_arbitrary_path_ignored(
        self, temp_agents_dir, no_redis, tmp_path, enable_local, monkeypatch
    ):
        # SECURITY: a forged persisted card pointing at an arbitrary dir must not
        # cause dispatch there — the package_path is re-derived from the scan,
        # which returns None for an id with no real package.
        evil = tmp_path / "evil"
        evil.mkdir()
        register_agent("x", transport="local", metadata={"package_path": str(evil)})
        monkeypatch.setattr("agentibridge.local_agents.get_local_agent", lambda agent_id, *a, **k: None)

        called = {"dispatch": False}

        async def fake_dispatch(**k):
            called["dispatch"] = True
            return {"dispatched": True, "job_id": "j", "status": "running"}

        monkeypatch.setattr("agentibridge.dispatch.dispatch_task", fake_dispatch)
        result = asyncio.run(route_to_agent("x", "task"))
        assert result["success"] is False
        assert "no local agent package" in result["error"]
        assert called["dispatch"] is False  # never spawned claude in the evil dir
