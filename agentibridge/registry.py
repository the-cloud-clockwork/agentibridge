"""Agent registry for A2A discovery.

Agents self-register on boot and send periodic heartbeats.
Any agent can discover peers via list_agents / find_agents.

Storage: Redis (primary) with file fallback — same pattern as dispatch.py.
"""

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentibridge.logging import log
from agentibridge.redis_client import get_redis

_AGENTS_DIR = Path("/tmp/agentibridge_agents")
_KEY_PREFIX: str = "agentibridge"
_DEFAULT_HEARTBEAT_TTL = 300  # 5 minutes


@dataclass
class AgentRecord:
    agent_id: str
    agent_name: str = ""
    agent_type: str = ""
    capabilities: list = field(default_factory=list)
    endpoint: str = ""
    transport: str = "http"
    status: str = "online"
    metadata: dict = field(default_factory=dict)
    registered_at: str = ""
    last_heartbeat: str = ""
    heartbeat_ttl: int = _DEFAULT_HEARTBEAT_TTL


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------


def _rkey(suffix: str) -> str:
    return f"{_KEY_PREFIX}:sb:{suffix}"


def _agent_path(agent_id: str) -> Path:
    return _AGENTS_DIR / f"{agent_id}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def _iso_to_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return _now_ts()


# ---------------------------------------------------------------------------
# Effective status (read-time projection, no writes)
# ---------------------------------------------------------------------------


def _compute_effective_status(data: dict) -> str:
    stored = data.get("status", "online")
    if stored == "offline":
        return "offline"
    try:
        last = _iso_to_ts(data.get("last_heartbeat", ""))
        ttl = int(data.get("heartbeat_ttl", _DEFAULT_HEARTBEAT_TTL))
        if (_now_ts() - last) > ttl:
            return "offline"
    except (KeyError, ValueError, TypeError):
        pass
    return stored


# ---------------------------------------------------------------------------
# File fallback
# ---------------------------------------------------------------------------


def _write_file(agent_id: str, data: dict) -> None:
    _AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    _agent_path(agent_id).write_text(json.dumps(data))


def _read_file(agent_id: str) -> Optional[dict]:
    path = _agent_path(agent_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _delete_file(agent_id: str) -> None:
    _agent_path(agent_id).unlink(missing_ok=True)


def _list_files(agent_type: str, capability: str, status: str, limit: int) -> List[dict]:
    if not _AGENTS_DIR.exists():
        return []
    agents: List[dict] = []
    files = sorted(_AGENTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        data["effective_status"] = _compute_effective_status(data)
        if agent_type and data.get("agent_type") != agent_type:
            continue
        if capability and capability not in data.get("capabilities", []):
            continue
        if status and data["effective_status"] != status:
            continue
        agents.append(data)
        if len(agents) >= limit:
            break
    return agents


# ---------------------------------------------------------------------------
# Redis storage
# ---------------------------------------------------------------------------


def _serialize(data: dict) -> dict:
    return {k: json.dumps(v) if not isinstance(v, str) else v for k, v in data.items()}


def _deserialize(raw: dict) -> dict:
    result = {}
    for k, v in raw.items():
        try:
            result[k] = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            result[k] = v
    return result


def _read_redis(agent_id: str) -> Optional[dict]:
    r = get_redis()
    if r is None:
        return None
    try:
        data = r.hgetall(_rkey(f"agent:{agent_id}"))
        if not data:
            return None
        return _deserialize(data)
    except Exception:
        return None


def _write_redis(agent_id: str, data: dict, ttl: int) -> None:
    r = get_redis()
    if r is None:
        return
    try:
        hash_key = _rkey(f"agent:{agent_id}")
        r.hset(hash_key, mapping=_serialize(data))
        r.expire(hash_key, ttl * 2)
        score = _iso_to_ts(data.get("last_heartbeat", ""))
        r.zadd(_rkey("idx:agents"), {agent_id: score})
        # Type index
        if data.get("agent_type"):
            r.zadd(_rkey(f"idx:agents:type:{data['agent_type']}"), {agent_id: score})
        # Capability indices
        for cap in data.get("capabilities", []):
            r.sadd(_rkey(f"idx:agents:cap:{cap}"), agent_id)
    except Exception as e:
        log("registry: Redis write failed", {"agent_id": agent_id, "error": str(e)})


def _delete_redis(agent_id: str, capabilities: List[str], agent_type: str) -> None:
    r = get_redis()
    if r is None:
        return
    try:
        r.delete(_rkey(f"agent:{agent_id}"))
        r.zrem(_rkey("idx:agents"), agent_id)
        if agent_type:
            r.zrem(_rkey(f"idx:agents:type:{agent_type}"), agent_id)
        for cap in capabilities:
            r.srem(_rkey(f"idx:agents:cap:{cap}"), agent_id)
    except Exception as e:
        log("registry: Redis delete failed", {"agent_id": agent_id, "error": str(e)})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_agent(
    agent_id: str,
    agent_name: str = "",
    agent_type: str = "",
    capabilities: Optional[List[str]] = None,
    endpoint: str = "",
    metadata: Optional[dict] = None,
    heartbeat_ttl: int = _DEFAULT_HEARTBEAT_TTL,
    transport: str = "http",
) -> dict:
    if not agent_id or not agent_id.strip():
        raise ValueError("agent_id is required and cannot be empty")
    agent_id = agent_id.strip()
    caps = capabilities or []
    meta = metadata or {}
    now = _now_iso()

    # Read existing to diff capabilities on re-register
    existing = _read_redis(agent_id) or _read_file(agent_id)
    if existing:
        old_caps = set(existing.get("capabilities", []))
        new_caps = set(caps)
        removed = old_caps - new_caps
        if removed:
            r = get_redis()
            if r is not None:
                try:
                    for cap in removed:
                        r.srem(_rkey(f"idx:agents:cap:{cap}"), agent_id)
                except Exception:
                    pass

    registered_at = existing.get("registered_at", now) if existing else now

    data = asdict(
        AgentRecord(
            agent_id=agent_id,
            agent_name=agent_name or agent_id,
            agent_type=agent_type,
            capabilities=caps,
            endpoint=endpoint,
            transport=transport,
            status="online",
            metadata=meta,
            registered_at=registered_at,
            last_heartbeat=now,
            heartbeat_ttl=heartbeat_ttl,
        )
    )

    _write_file(agent_id, data)
    _write_redis(agent_id, data, heartbeat_ttl)

    log("registry: agent registered", {"agent_id": agent_id, "capabilities": len(caps)})
    return {"agent_id": agent_id, "registered": True}


def deregister_agent(agent_id: str) -> dict:
    existing = _read_redis(agent_id) or _read_file(agent_id)
    if not existing:
        return {"agent_id": agent_id, "deleted": False, "reason": "not found"}

    caps = existing.get("capabilities", [])
    agent_type = existing.get("agent_type", "")
    _delete_redis(agent_id, caps, agent_type)
    _delete_file(agent_id)

    log("registry: agent deregistered", {"agent_id": agent_id})
    return {"agent_id": agent_id, "deleted": True}


def heartbeat_agent(
    agent_id: str,
    status: str = "online",
    metadata: Optional[dict] = None,
) -> dict:
    existing = _read_redis(agent_id) or _read_file(agent_id)
    if not existing:
        return {"agent_id": agent_id, "success": False, "reason": "not registered"}

    now = _now_iso()
    existing["last_heartbeat"] = now
    existing["status"] = status
    if metadata:
        existing_meta = existing.get("metadata", {})
        if isinstance(existing_meta, str):
            try:
                existing_meta = json.loads(existing_meta)
            except Exception:
                existing_meta = {}
        existing_meta.update(metadata)
        existing["metadata"] = existing_meta

    ttl = int(existing.get("heartbeat_ttl", _DEFAULT_HEARTBEAT_TTL))
    _write_file(agent_id, existing)
    _write_redis(agent_id, existing, ttl)

    return {"agent_id": agent_id, "last_heartbeat": now, "status": status}


def get_agent(agent_id: str) -> Optional[dict]:
    data = _read_redis(agent_id)
    if data is None:
        data = _read_file(agent_id)
    if data is None:
        # Fall back to computed (session-gated) local agents — never persisted.
        try:
            from agentibridge.local_agents import get_local_agent

            return get_local_agent(agent_id)
        except Exception as e:  # pragma: no cover - defensive
            log("registry: local get_agent failed", {"agent_id": agent_id, "error": str(e)})
            return None
    data["effective_status"] = _compute_effective_status(data)
    return data


def _list_registered(
    agent_type: str = "",
    capability: str = "",
    status: str = "",
    limit: int = 50,
) -> List[dict]:
    # Try Redis
    r = get_redis()
    if r is not None:
        try:
            # Choose the right index
            if capability:
                agent_ids = list(r.smembers(_rkey(f"idx:agents:cap:{capability}")))
            elif agent_type:
                agent_ids = r.zrevrange(_rkey(f"idx:agents:type:{agent_type}"), 0, -1)
            else:
                agent_ids = r.zrevrange(_rkey("idx:agents"), 0, -1)

            agents: List[dict] = []
            for aid in agent_ids:
                data = _read_redis(aid)
                if data is None:
                    continue
                data["effective_status"] = _compute_effective_status(data)
                # Apply remaining filters
                if agent_type and data.get("agent_type") != agent_type:
                    continue
                if capability and capability not in data.get("capabilities", []):
                    continue
                if status and data["effective_status"] != status:
                    continue
                agents.append(data)
                if len(agents) >= limit:
                    break
            return agents
        except Exception as e:
            log("registry: Redis list_agents failed", {"error": str(e)})

    # File fallback
    return _list_files(agent_type, capability, status, limit)


def _merge_local_agents(
    agents: List[dict],
    agent_type: str,
    capability: str,
    status: str,
    limit: int,
) -> List[dict]:
    """Append computed (session-gated) local agents, deduped by agent_id.

    Registered agents (Redis/file) take precedence on id collision. No-op when
    the local-agents feature is disabled — discover_local_agents returns [].
    """
    try:
        from agentibridge.local_agents import discover_local_agents, filter_records

        local = filter_records(discover_local_agents(), agent_type, capability, status)
    except Exception as e:  # pragma: no cover - defensive
        log("registry: local agent merge failed", {"error": str(e)})
        return agents

    if not local:
        return agents[:limit]

    existing = {a.get("agent_id") for a in agents}
    fresh = [rec for rec in local if rec.get("agent_id") not in existing]
    if not fresh:
        return agents[:limit]

    if len(agents) + len(fresh) <= limit:
        return agents + fresh  # common case — everyone fits under the cap

    # Over the cap: local packages are a small, bounded set and are the feature
    # being surfaced here, so guarantee them a place rather than letting a full
    # registered slice silently starve them (route_by_capability funnels through
    # here too). Registered agents keep priority for the remaining slots.
    reserved = min(len(fresh), limit)
    keep_registered = agents[: max(0, limit - reserved)]
    return keep_registered + fresh[:reserved]


def list_agents(
    agent_type: str = "",
    capability: str = "",
    status: str = "",
    limit: int = 50,
) -> List[dict]:
    agents = _list_registered(agent_type, capability, status, limit)
    return _merge_local_agents(agents, agent_type, capability, status, limit)


def find_agents(capability: str) -> List[dict]:
    return list_agents(capability=capability)


# ---------------------------------------------------------------------------
# Agent routing — forward tasks to discovered agents
# ---------------------------------------------------------------------------


async def _route_to_local_agent(agent_id: str, task: str, *, wait: bool = False) -> dict:
    """Deliver a task to a local agent by spawning a fresh ``claude`` in its dir.

    ``wait=False`` (default) fire-and-forgets via ``dispatch_task`` and returns a
    job_id immediately. ``wait=True`` blocks via ``run_claude`` until the run
    completes and returns its result. Cold-start is always allowed here (liveness
    is advisory), but two guards are NOT optional:

    - Hard-gated on ``AGENTIBRIDGE_LOCAL_AGENTS_ENABLED`` — a persisted
      ``transport=local`` card cannot enable dispatch while the feature is off.
    - The package directory is re-derived from the filesystem scan via
      ``local_agents.get_local_agent`` (which only returns real, contained
      ``<hub>/agents/<id>/package`` dirs), NOT taken from the persisted record's
      ``metadata.package_path``. This prevents a forged registry entry from
      running claude in an arbitrary host directory.
    """
    import os

    from agentibridge import local_agents

    if not local_agents.local_agents_enabled():
        return {
            "success": False,
            "error": "local agents are disabled (set AGENTIBRIDGE_LOCAL_AGENTS_ENABLED=true)",
            "agent_id": agent_id,
        }
    if not task or not task.strip():
        return {"success": False, "error": "task is required", "agent_id": agent_id}

    resolved = local_agents.get_local_agent(agent_id)
    if resolved is None:
        return {"success": False, "error": f"no local agent package for id: {agent_id}", "agent_id": agent_id}
    package_path = resolved.get("metadata", {}).get("package_path", "")
    if not package_path or not Path(package_path).is_dir():
        return {"success": False, "error": f"local agent has no valid package_path: {agent_id}", "agent_id": agent_id}

    try:
        if wait:
            from agentibridge.claude_runner import run_claude

            timeout_s = int(float(os.getenv("AGENTIBRIDGE_AGENT_TIMEOUT", "600")))
            res = await run_claude(prompt=task, cwd=package_path, timeout=timeout_s)
            return {
                "success": res.success,
                "agent_id": agent_id,
                "transport": "local",
                "package_path": package_path,
                "result": res.result,
                "session_id": res.session_id,
                "timed_out": res.timed_out,
                "error": res.error,
            }

        from agentibridge.dispatch import dispatch_task

        result = await dispatch_task(task_description=task, project=package_path)
        return {"success": True, "agent_id": agent_id, "transport": "local", "package_path": package_path, **result}
    except Exception as e:
        log("registry: local dispatch failed", {"agent_id": agent_id, "error": str(e)})
        return {"success": False, "error": str(e), "agent_id": agent_id}


async def route_to_agent(
    agent_id: str,
    task: str,
    profile: str = "",
    repo_url: str = "",
    wait: bool = False,
    file_path: str = "",
) -> dict:
    """Forward a task to a specific agent's REST API."""
    import httpx

    agent = get_agent(agent_id)
    if agent is None:
        return {"success": False, "error": f"agent not found: {agent_id}"}

    # Local (session-gated package) agents: spawn a fresh claude in the package
    # dir. A cold start is always allowed, so we branch BEFORE the online /
    # capacity / endpoint checks below — liveness is advisory for discovery only.
    if agent.get("transport") == "local":
        return await _route_to_local_agent(agent_id, task, wait=wait)

    if agent.get("effective_status") != "online":
        return {"success": False, "error": f"agent offline: {agent_id}"}

    meta = agent.get("metadata", {})
    capacity = meta.get("available_capacity", 1)
    if capacity <= 0:
        return {"success": False, "error": "agent at capacity", "retry": True, "agent_id": agent_id}

    endpoint = agent.get("endpoint", "")
    if not endpoint:
        return {"success": False, "error": f"agent has no endpoint: {agent_id}"}

    body: Dict[str, Any] = {"task": task, "wait": wait}
    if profile:
        body["profile"] = profile
    if repo_url:
        body["repo_url"] = repo_url
    if file_path:
        body["file_path"] = file_path

    # Long-running agents (claude-code subprocesses) routinely take 1-10 min.
    # When wait=True, we block until the agent finishes — give it 10 min.
    # When wait=False, the agent returns a job_id immediately, 30s is enough.
    import os

    timeout_s = float(os.getenv("AGENTIBRIDGE_AGENT_TIMEOUT", "600" if wait else "30"))

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(f"{endpoint}/jobs", json=body)
            if resp.status_code == 503:
                return {"success": False, "error": "agent at capacity", "retry": True, "agent_id": agent_id}
            resp.raise_for_status()
            data = resp.json()
            log("registry: routed task to agent", {"agent_id": agent_id, "job_id": data.get("job", {}).get("id", "")})
            return {"success": True, "agent_id": agent_id, "endpoint": endpoint, **data}
    except httpx.ConnectError:
        return {"success": False, "error": f"agent unreachable: {endpoint}", "agent_id": agent_id}
    except httpx.TimeoutException:
        return {"success": False, "error": f"agent timeout: {endpoint}", "agent_id": agent_id}
    except Exception as e:
        return {"success": False, "error": str(e), "agent_id": agent_id}


async def route_by_capability(
    capability: str,
    task: str,
    profile: str = "",
    repo_url: str = "",
    wait: bool = False,
    file_path: str = "",
) -> dict:
    """Find best agent for a capability and forward the task."""
    agents = find_agents(capability)

    # HTTP agents must be online to serve — an offline pod cannot take work.
    # Local (session-gated) agents stay candidates while offline: "offline" for
    # them only means no live claude session, and dispatch cold-starts one. This
    # keeps capability routing consistent with direct run_agent, which also
    # cold-starts.
    candidates = [a for a in agents if a.get("effective_status") == "online" or a.get("transport") == "local"]
    if not candidates:
        return {"success": False, "error": f"no available agents with capability: {capability}", "retry": True}

    # Prefer warm (online) agents, then the most available capacity.
    candidates.sort(
        key=lambda a: (
            a.get("effective_status") == "online",
            a.get("metadata", {}).get("available_capacity", 0),
        ),
        reverse=True,
    )

    best = candidates[0]
    # The capacity gate applies to HTTP agents; a local cold-start has no queue.
    if best.get("transport") != "local":
        capacity = best.get("metadata", {}).get("available_capacity", 0)
        if capacity <= 0:
            return {"success": False, "error": "all agents at capacity", "retry": True}

    result = await route_to_agent(
        agent_id=best["agent_id"],
        task=task,
        profile=profile,
        repo_url=repo_url,
        wait=wait,
        file_path=file_path,
    )
    result["routed_by"] = "capability"
    result["capability"] = capability
    return result
