"""Unit tests for local agent discovery (session-gated packages)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agentibridge.local_agents as la
from agentibridge.local_agents import encode_project_path, filter_records, resolve_agentihub_dir


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class FakeStore:
    """Minimal stand-in for SessionStore.list_sessions."""

    def __init__(self, sessions):
        self._sessions = sessions
        self.calls = []

    def list_sessions(self, project=None, limit=20, offset=0, since_hours=0):
        self.calls.append(project)
        return list(self._sessions)


def _session(project_encoded, last_update, session_id="sess-1"):
    return SimpleNamespace(
        project_encoded=project_encoded,
        last_update=last_update,
        session_id=session_id,
    )


def _make_hub(tmp_path, names):
    hub = tmp_path / "agentihub"
    for name in names:
        pkg = hub / "agents" / name / "package"
        pkg.mkdir(parents=True)
        (pkg / "CLAUDE.md").write_text(f"# {name}\n")
    return hub


@pytest.fixture(autouse=True)
def _reset_hub_cache():
    la.reset_caches()
    yield
    la.reset_caches()


@pytest.fixture
def enable_local(monkeypatch):
    monkeypatch.setattr("agentibridge.config.AGENTIBRIDGE_LOCAL_AGENTS_ENABLED", True)


def _pkg_encoded(hub, name):
    # discover_local_agents resolves package paths, so the session's
    # project_encoded must be computed from the resolved path to match.
    return encode_project_path(str((hub / "agents" / name / "package").resolve()))


def _write_manifest(hub, name, text):
    (hub / "agents" / name / "package" / "command.yml").write_text(text)


@pytest.mark.unit
class TestPackageManifest:
    def test_capabilities_and_description_from_manifest(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["finops"])
        _write_manifest(
            hub,
            "finops",
            "name: FinOps\ndescription: AWS cost and billing analysis\n"
            "capabilities:\n  - cost-analysis\n  - billing\nclaude:\n  model: opus\n",
        )
        rec = la.get_local_agent("finops", str(hub), store=FakeStore([]), ttl=3600)
        assert rec["agent_name"] == "FinOps"
        assert rec["metadata"]["description"] == "AWS cost and billing analysis"
        assert rec["capabilities"] == ["local", "agent:finops", "cost-analysis", "billing"]

    def test_missing_manifest_falls_back_to_base_tags(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["alpha"])
        rec = la.get_local_agent("alpha", str(hub), store=FakeStore([]), ttl=3600)
        assert rec["capabilities"] == ["local", "agent:alpha"]
        assert rec["agent_name"] == "alpha"
        assert rec["metadata"]["description"] == ""

    def test_claude_only_manifest_is_fine(self, tmp_path, enable_local):
        # This is what 7 of the 8 real packages look like today.
        hub = _make_hub(tmp_path, ["alpha"])
        _write_manifest(hub, "alpha", "claude:\n  model: opus\n  max_turns: 100\n")
        rec = la.get_local_agent("alpha", str(hub), store=FakeStore([]), ttl=3600)
        assert rec["capabilities"] == ["local", "agent:alpha"]

    def test_malformed_yaml_does_not_break_discovery(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["alpha"])
        _write_manifest(hub, "alpha", "name: [unclosed\n  bad: :\n")
        rec = la.get_local_agent("alpha", str(hub), store=FakeStore([]), ttl=3600)
        assert rec is not None  # still discoverable
        assert rec["capabilities"] == ["local", "agent:alpha"]

    def test_non_list_capabilities_ignored(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["alpha"])
        _write_manifest(hub, "alpha", "capabilities: not-a-list\nclaude:\n  model: opus\n")
        rec = la.get_local_agent("alpha", str(hub), store=FakeStore([]), ttl=3600)
        assert rec["capabilities"] == ["local", "agent:alpha"]

    def test_capability_routing_matches_declared_tag(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["finops", "linkedin"])
        _write_manifest(hub, "finops", "capabilities:\n  - cost-analysis\n")
        _write_manifest(hub, "linkedin", "capabilities:\n  - content-publishing\n")
        recs = la.discover_local_agents(str(hub), store=FakeStore([]), ttl=3600)
        hits = filter_records(recs, capability="cost-analysis")
        assert [h["agent_id"] for h in hits] == ["finops"]


@pytest.mark.unit
class TestEncodeProjectPath:
    def test_basic(self):
        assert encode_project_path("/home/u/dev/x") == "-home-u-dev-x"

    def test_trailing_slash_stripped(self):
        assert encode_project_path("/home/u/dev/x/") == "-home-u-dev-x"

    def test_dashed_segments_preserved(self):
        # coding-agent must NOT be split — this is exactly why we encode forward
        # rather than reuse parser.decode_project_path (which is dash-lossy).
        p = "/home/u/dev/tcc-ecosystem/agentihub/agents/coding-agent/package"
        assert encode_project_path(p) == "-home-u-dev-tcc-ecosystem-agentihub-agents-coding-agent-package"


@pytest.mark.unit
class TestResolveAgentihubDir:
    def test_explicit_dir_with_agents(self, tmp_path):
        hub = _make_hub(tmp_path, ["a"])
        assert resolve_agentihub_dir(str(hub)) == hub

    def test_explicit_dir_without_agents_not_returned(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        # No agents/ subdir -> this exact dir must not be the resolution.
        assert resolve_agentihub_dir(str(empty)) != empty


@pytest.mark.unit
class TestLocalAgentsGate:
    def test_disabled_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agentibridge.config.AGENTIBRIDGE_LOCAL_AGENTS_ENABLED", False)
        hub = _make_hub(tmp_path, ["a"])
        assert la.discover_local_agents(str(hub), store=FakeStore([]), ttl=3600) == []

    def test_enabled_flag(self, monkeypatch):
        monkeypatch.setattr("agentibridge.config.AGENTIBRIDGE_LOCAL_AGENTS_ENABLED", True)
        assert la.local_agents_enabled() is True


@pytest.mark.unit
class TestDiscoverLocalAgents:
    def test_scans_packages_with_claude_md(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["alpha", "beta"])
        # A dir without package/CLAUDE.md is skipped.
        (hub / "agents" / "nope").mkdir()
        recs = la.discover_local_agents(str(hub), store=FakeStore([]), ttl=3600)
        assert sorted(r["agent_id"] for r in recs) == ["alpha", "beta"]
        for r in recs:
            assert r["transport"] == "local"
            assert r["agent_type"] == "local"
            assert r["endpoint"] == ""
            assert r["metadata"]["package_path"].endswith("/package")
            assert f"agent:{r['agent_id']}" in r["capabilities"]

    def test_offline_when_no_session(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["alpha"])
        recs = la.discover_local_agents(str(hub), store=FakeStore([]), ttl=3600)
        assert recs[0]["effective_status"] == "offline"
        assert recs[0]["metadata"]["available_capacity"] == 0

    def test_online_when_recent_session(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["coding-agent"])
        encoded = _pkg_encoded(hub, "coding-agent")
        store = FakeStore([_session(encoded, _iso(datetime.now(timezone.utc)))])
        recs = la.discover_local_agents(str(hub), store=store, ttl=3600)
        assert recs[0]["effective_status"] == "online"
        assert recs[0]["metadata"]["available_capacity"] == 1
        assert recs[0]["metadata"]["session_id"] == "sess-1"

    def test_offline_when_session_stale(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["alpha"])
        encoded = _pkg_encoded(hub, "alpha")
        old = datetime.now(timezone.utc) - timedelta(seconds=7200)
        store = FakeStore([_session(encoded, _iso(old))])
        recs = la.discover_local_agents(str(hub), store=store, ttl=3600)
        assert recs[0]["effective_status"] == "offline"

    def test_dashed_name_requires_exact_match(self, tmp_path, enable_local):
        # A session under any non-exact project_encoded must NOT count as live —
        # proves we do not rely on the lossy decode for dashed agent names.
        hub = _make_hub(tmp_path, ["coding-agent"])
        decoy = _pkg_encoded(hub, "coding-agent").replace("coding-agent", "coding/agent")
        store = FakeStore([_session(decoy, _iso(datetime.now(timezone.utc)))])
        recs = la.discover_local_agents(str(hub), store=store, ttl=3600)
        assert recs[0]["effective_status"] == "offline"

    def test_empty_last_update_is_offline(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["alpha"])
        encoded = _pkg_encoded(hub, "alpha")
        store = FakeStore([_session(encoded, "")])
        recs = la.discover_local_agents(str(hub), store=store, ttl=3600)
        assert recs[0]["effective_status"] == "offline"


@pytest.mark.unit
class TestGetAndFilter:
    def test_get_local_agent(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["alpha", "beta"])
        rec = la.get_local_agent("beta", str(hub), store=FakeStore([]), ttl=3600)
        assert rec is not None
        assert rec["agent_id"] == "beta"
        assert la.get_local_agent("ghost", str(hub), store=FakeStore([]), ttl=3600) is None

    def test_get_local_agent_disabled_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agentibridge.config.AGENTIBRIDGE_LOCAL_AGENTS_ENABLED", False)
        hub = _make_hub(tmp_path, ["alpha"])
        assert la.get_local_agent("alpha", str(hub), store=FakeStore([]), ttl=3600) is None

    def test_get_local_agent_rejects_traversal(self, tmp_path, enable_local):
        hub = _make_hub(tmp_path, ["alpha"])
        assert la.get_local_agent("../alpha", str(hub), store=FakeStore([]), ttl=3600) is None
        assert la.get_local_agent("a/b", str(hub), store=FakeStore([]), ttl=3600) is None

    def test_filter_records(self):
        recs = [
            {
                "agent_id": "a",
                "agent_type": "local",
                "capabilities": ["local", "agent:a"],
                "effective_status": "online",
            },
            {
                "agent_id": "b",
                "agent_type": "local",
                "capabilities": ["local", "agent:b"],
                "effective_status": "offline",
            },
        ]
        assert len(filter_records(recs, status="online")) == 1
        assert len(filter_records(recs, capability="agent:b")) == 1
        assert len(filter_records(recs, agent_type="local")) == 2
        assert len(filter_records(recs, agent_type="executor")) == 0
