"""Local agent discovery for A2A — session-gated package agents.

A **local agent** is a purpose-built Claude Code agent package on disk under an
AgentiHub checkout: ``<AGENTIHUB_DIR>/agents/<name>/package/`` containing a
``CLAUDE.md``. This is the same contract agenticore's ``agents_tui`` uses to
discover local agents.

Unlike **service agents** (which self-register with an HTTP ``endpoint`` and
send heartbeats), local agents are:

- **Discovered**, never persisted — the catalog is a filesystem scan performed
  at read time. Nothing is written to Redis / the agent file store.
- **Session-gated** — a package's *warmth* is derived from whether a live
  ``claude`` session's working directory maps to the package path within a
  freshness TTL. No session running there → ``idle``, NOT ``offline``. A local
  agent is never unreachable: dispatch cold-starts a fresh claude in it. Status
  conveys warmth, never reachability, and must never read as "do not call".

Each package is shaped as an ``AgentRecord``-compatible dict (``transport =
"local"``, empty ``endpoint``, ``package_path`` in ``metadata``) so it flows
through the registry read paths (``list_agents`` / ``get_agent`` /
``find_agents``) unchanged. Dispatch is handled by ``registry.route_to_agent``,
which branches on ``transport == "local"`` and spawns a fresh ``claude`` in the
package directory — a cold start is always allowed, so liveness is advisory for
discovery only, never a hard gate on direct dispatch.

Design references: [[feedback_profile_ownership]] (packages belong to
agentihub), agenticore ``agents_tui.discover_local_agents``.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from agentibridge.logging import log

LOCAL_AGENT_TYPE = "local"
LOCAL_TRANSPORT = "local"

# Memoized hub resolution — the resolved path cannot change within a process,
# and the sibling-walk fallback is an unbounded directory scan we must not repeat
# on every list_agents/get_agent call. Keyed by (explicit arg, config value).
_HUB_CACHE: dict = {}


# ---------------------------------------------------------------------------
# Path encoding — absolute path -> Claude Code project directory name
# ---------------------------------------------------------------------------


def encode_project_path(path: str) -> str:
    """Encode an absolute path to a Claude Code ``~/.claude/projects`` dir name.

    Claude Code's CLI creates the project directory by replacing every ``/`` in
    the absolute cwd with ``-`` (e.g. ``/home/u/dev/x`` -> ``-home-u-dev-x``).
    We reproduce that forward transform here.

    This is deliberately NOT the inverse of ``parser.decode_project_path`` —
    decoding is lossy for path segments that themselves contain a dash
    (``coding-agent``, ``tcc-ecosystem``). Encoding forward is lossless, and we
    only ever need the forward direction to look a package up by its exact
    ``project_encoded`` name.
    """
    return str(path).rstrip("/").replace("/", "-")


# ---------------------------------------------------------------------------
# AgentiHub resolution (mirrors agenticore's 4-tier resolver, minus state file)
# ---------------------------------------------------------------------------


def resolve_agentihub_dir(agentihub_dir: str = "") -> Optional[Path]:
    """Resolve the AgentiHub root that contains an ``agents/`` subdir.

    Precedence: explicit arg -> ``AGENTIHUB_DIR`` env/config -> sibling-directory
    discovery (walk up from this file looking for an ``agentihub/agents/`` dir).
    Returns ``None`` when nothing resolves. Survives ecosystem-dir renames the
    same way agenticore's resolver does.

    The result is ``.resolve()``-d (symlink-free, canonical) so the encoded
    project name derived from a package path matches the OS-reported cwd of a
    live claude session (getcwd() is always symlink-resolved). Result is memoized
    per process — see ``_HUB_CACHE``.
    """
    from agentibridge.config import AGENTIHUB_DIR

    cache_key = (agentihub_dir, AGENTIHUB_DIR)
    if cache_key in _HUB_CACHE:
        return _HUB_CACHE[cache_key]

    result = _resolve_agentihub_dir_uncached(agentihub_dir, AGENTIHUB_DIR)
    _HUB_CACHE[cache_key] = result
    return result


def _resolve_agentihub_dir_uncached(agentihub_dir: str, config_hub: str) -> Optional[Path]:
    candidates: List[Path] = []
    if agentihub_dir:
        candidates.append(Path(agentihub_dir).expanduser())
    if config_hub:
        candidates.append(Path(config_hub).expanduser())

    for c in candidates:
        if c.is_dir() and (c / "agents").is_dir():
            return c.resolve()

    # Sibling-discovery fallback: walk up from this file looking for an
    # `agentihub/agents/` next to us or under an intermediate parent.
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "agentihub"
        if (cand / "agents").is_dir():
            return cand.resolve()
        for sibling in parent.glob("*/agentihub/agents"):
            return sibling.parent.resolve()
    return None


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


def local_agents_enabled() -> bool:
    """True when local agent discovery/dispatch is enabled (config toggle)."""
    from agentibridge.config import AGENTIBRIDGE_LOCAL_AGENTS_ENABLED

    return AGENTIBRIDGE_LOCAL_AGENTS_ENABLED


# ---------------------------------------------------------------------------
# Session liveness — is a claude session live in this package dir?
# ---------------------------------------------------------------------------


def reset_caches() -> None:
    """Clear memoized hub resolution. For tests / after config changes."""
    _HUB_CACHE.clear()


def _get_store():
    # Fresh instance per call — SessionStore.__init__ does no I/O. Note the
    # underlying Redis client (agentibridge.redis_client.get_redis) is a
    # process-global singleton, so this is cheap; it does NOT re-attempt a
    # connection that already failed earlier in the process.
    from agentibridge.store import SessionStore

    return SessionStore()


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _session_liveness(package_path: str, store, ttl: int) -> Tuple[str, str, str]:
    """Return ``(status, last_activity_iso, session_id)`` for a package path.

    ``status`` is ``"online"`` iff a claude session whose ``project_encoded``
    exactly matches ``encode_project_path(package_path)`` had activity within
    ``ttl`` seconds, else ``"idle"``. Exact-match (not the store's lossy
    substring/decode) avoids false positives on dashed agent names.

    NOTE the vocabulary: a local agent is never ``"offline"``. In an agent
    registry "offline" means *unreachable*, and a local package is always
    reachable — dispatch cold-starts a fresh claude in it. ``"idle"`` means
    only "no live session right now". Emitting "offline" here made LLM callers
    hedge or refuse to dispatch, even though the call would have succeeded.
    """
    encoded = encode_project_path(package_path)
    try:
        # Substring pre-filter narrows the candidate set; we exact-match below.
        sessions = store.list_sessions(project=encoded, limit=50)
    except Exception as e:  # pragma: no cover - defensive
        log("local_agents: liveness query failed", {"package_path": package_path, "error": str(e)})
        return ("idle", "", "")

    now = datetime.now(timezone.utc)
    best: Optional[Tuple[datetime, object]] = None
    for s in sessions:
        if getattr(s, "project_encoded", "") != encoded:
            continue
        dt = _parse_iso(getattr(s, "last_update", ""))
        if dt is None:
            continue
        if best is None or dt > best[0]:
            best = (dt, s)

    if best is None:
        return ("idle", "", "")

    dt, meta = best
    age = (now - dt).total_seconds()
    status = "online" if age <= ttl else "idle"
    return (status, getattr(meta, "last_update", ""), getattr(meta, "session_id", ""))


# ---------------------------------------------------------------------------
# Record shaping + discovery
# ---------------------------------------------------------------------------


def read_package_manifest(package_path) -> dict:
    """Read ``package/command.yml`` -> ``{display_name, description, capabilities}``.

    This is where a local agent's *real* capabilities come from: the package
    declares domain tags (``cost-analysis``, ``video-editing``, …) that an
    orchestrator routes on via ``find_agents`` / ``dispatch_to_agent``.

    Best-effort by design — a missing, unreadable, or malformed manifest yields
    ``{}`` and the agent stays discoverable with its base tags. Package authoring
    must never be able to break discovery.
    """
    manifest = Path(package_path) / "command.yml"
    if not manifest.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(manifest.read_text()) or {}
    except Exception as e:
        log("local_agents: command.yml parse failed", {"package_path": str(package_path), "error": str(e)})
        return {}

    if not isinstance(data, dict):
        return {}

    caps = data.get("capabilities")
    if not isinstance(caps, list):
        caps = []

    return {
        "display_name": str(data.get("name") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "capabilities": [str(c).strip() for c in caps if str(c).strip()],
    }


def _build_record(
    name: str,
    package_path: str,
    status: str,
    last_activity: str,
    session_id: str,
    ttl: int,
    manifest: Optional[dict] = None,
) -> dict:
    online = status == "online"
    manifest = manifest or {}

    # Base identity tags are always present; declared domain tags are appended
    # (order-preserving, deduped) so find_agents("cost-analysis") can match.
    caps = [LOCAL_TRANSPORT, f"agent:{name}"]
    for c in manifest.get("capabilities", []):
        if c not in caps:
            caps.append(c)

    # A local agent is ALWAYS callable — dispatch cold-starts a fresh claude in
    # its package dir. So it always advertises capacity and dispatchable=True.
    # `status` conveys warmth only: "online" = a live session is running,
    # "idle" = none right now (a cold start will be spawned on dispatch).
    # Reporting capacity 0 / "offline" for an idle agent previously caused LLM
    # callers to hedge ("this may fail to reach it") on calls that in fact work.
    return {
        "agent_id": name,
        "agent_name": manifest.get("display_name") or name,
        "agent_type": LOCAL_AGENT_TYPE,
        "capabilities": caps,
        "endpoint": "",
        "transport": LOCAL_TRANSPORT,
        "status": status,
        "dispatchable": True,
        "metadata": {
            "source": "agentihub",
            "package_path": package_path,
            "description": manifest.get("description", ""),
            "available_capacity": 1,
            "session_live": online,
            "cold_start_on_dispatch": not online,
            "dispatch_hint": (
                "Live session running — dispatch reaches it immediately."
                if online
                else "No live session. Dispatch is still fully supported: it cold-starts a "
                "fresh claude in the package dir. Do NOT treat 'idle' as unreachable."
            ),
            "session_id": session_id,
            "last_activity": last_activity,
        },
        "registered_at": "",
        "last_heartbeat": last_activity,
        "heartbeat_ttl": ttl,
        "effective_status": status,
    }


def discover_local_agents(agentihub_dir: str = "", *, store=None, ttl: Optional[int] = None) -> List[dict]:
    """Scan the AgentiHub for local agent packages, with session-gated liveness.

    Returns a list of ``AgentRecord``-shaped dicts. Empty list when the feature
    is disabled, no hub resolves, or the hub has no ``agents/`` subdir — never
    raises for those cases.
    """
    if not local_agents_enabled():
        return []

    hub = resolve_agentihub_dir(agentihub_dir)
    if hub is None:
        return []

    agents_dir = hub / "agents"
    if not agents_dir.is_dir():
        return []

    if ttl is None:
        from agentibridge.config import AGENTIBRIDGE_LOCAL_SESSION_TTL

        ttl = AGENTIBRIDGE_LOCAL_SESSION_TTL
    if store is None:
        store = _get_store()

    records: List[dict] = []
    for entry in sorted(agents_dir.iterdir()):
        if not entry.is_dir():
            continue
        package_path = entry / "package"
        # An agent is any dir containing package/CLAUDE.md (agenticore contract).
        if not (package_path / "CLAUDE.md").is_file():
            continue
        pkg = str(package_path.resolve())
        status, last_activity, session_id = _session_liveness(pkg, store, ttl)
        manifest = read_package_manifest(pkg)
        records.append(_build_record(entry.name, pkg, status, last_activity, session_id, ttl, manifest))

    return records


def _safe_agent_id(agent_id: str) -> bool:
    """Reject ids that are not a single, traversal-free path component."""
    return bool(agent_id) and agent_id not in (".", "..") and "/" not in agent_id and "\\" not in agent_id


def get_local_agent(agent_id: str, agentihub_dir: str = "", *, store=None, ttl: Optional[int] = None) -> Optional[dict]:
    """Return a single local agent record by id, or ``None`` if not found.

    Resolves the one candidate package directly (``<hub>/agents/<id>/package``)
    rather than scanning the whole hub — and only returns a record for a real,
    contained package (dir under the resolved hub, containing CLAUDE.md). This is
    also the security boundary for local dispatch: registry routes tasks using
    the package_path returned here, never a caller-supplied one.
    """
    if not local_agents_enabled():
        return None
    if not _safe_agent_id(agent_id):
        return None

    hub = resolve_agentihub_dir(agentihub_dir)
    if hub is None:
        return None

    package_path = hub / "agents" / agent_id / "package"
    if not (package_path / "CLAUDE.md").is_file():
        return None
    pkg_resolved = package_path.resolve()
    # Defense in depth: the resolved package must stay within the resolved hub.
    if hub not in pkg_resolved.parents:
        return None

    if ttl is None:
        from agentibridge.config import AGENTIBRIDGE_LOCAL_SESSION_TTL

        ttl = AGENTIBRIDGE_LOCAL_SESSION_TTL
    if store is None:
        store = _get_store()

    pkg = str(pkg_resolved)
    status, last_activity, session_id = _session_liveness(pkg, store, ttl)
    manifest = read_package_manifest(pkg)
    return _build_record(agent_id, pkg, status, last_activity, session_id, ttl, manifest)


def filter_records(records: List[dict], agent_type: str = "", capability: str = "", status: str = "") -> List[dict]:
    """Apply the same agent_type/capability/status filters registry.list_agents uses."""
    out: List[dict] = []
    for rec in records:
        if agent_type and rec.get("agent_type") != agent_type:
            continue
        if capability and capability not in rec.get("capabilities", []):
            continue
        if status and rec.get("effective_status") != status:
            continue
        out.append(rec)
    return out
