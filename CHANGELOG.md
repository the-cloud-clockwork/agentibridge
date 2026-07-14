# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] - 2026-07-14

### Added — Phase 6: Session-gated local agents (A2A)

Local "purpose-built" agent packages from an AgentiHub checkout are now
first-class A2A citizens: discoverable, routable by capability, and callable
whether or not a session is already running in them.

- **`local_agents.py`** (new) — discovers agent packages at
  `<AGENTIHUB_DIR>/agents/<name>/package/CLAUDE.md`. Local agents are *computed*
  at read time (filesystem scan + session-store liveness), never persisted.
- **Session-gated liveness** — a package is `online` only while a live `claude`
  session's cwd maps to it (within `AGENTIBRIDGE_LOCAL_SESSION_TTL`), otherwise
  `idle`. Derived from the existing session store; no heartbeats required.
- **`transport` field on `AgentRecord`** (default `"http"`, back-compatible).
  `transport="local"` dispatches by spawning a fresh `claude` in the package dir.
- **Capability tags** — read from each package's `command.yml` (`capabilities`,
  `name`, `description`), so `find_agents(capability=…)` and
  `dispatch_to_agent(capability=…)` route on what an agent actually does.
  Malformed/missing manifests degrade gracefully to base tags.
- **`discover_local_agents` MCP tool** — lists local agents, marks any shadowed
  by a same-id registered record.
- New config: `AGENTIBRIDGE_LOCAL_AGENTS_ENABLED` (default `false`),
  `AGENTIHUB_DIR`, `AGENTIBRIDGE_LOCAL_SESSION_TTL` (default 3600).
- New dependency: `pyyaml` (was previously only transitively available).

### Changed

- **`route_by_capability`** — local agents remain candidates while `idle`;
  dispatch cold-starts them. HTTP agents still require `online`. Warm (online)
  agents are preferred, then capacity.
- **Local agents report `idle`, never `offline`.** In an agent registry
  "offline" means *unreachable* and `available_capacity: 0` means *at capacity* —
  LLM callers read those literally and refused/hedged on dispatches that in fact
  succeed. Local agents are never unreachable, so status now conveys *warmth*
  only (`online` / `idle`), and records advertise `dispatchable: true`,
  `available_capacity: 1`, `session_live`, `cold_start_on_dispatch`, and a
  `dispatch_hint`. Tool descriptions state this explicitly, since LLM callers
  read them before deciding whether to call.

### Security

- Local dispatch is hard-gated on `AGENTIBRIDGE_LOCAL_AGENTS_ENABLED` and
  **re-derives the package path from the filesystem scan**, never trusting a
  persisted record's `metadata.package_path`. Without this, a forged
  `register_agent(transport="local", metadata={"package_path": "/"})` card could
  run `claude --dangerously-skip-permissions` in an arbitrary host directory.
  Package paths are containment-checked against the resolved AgentiHub root and
  agent ids are rejected if not a single traversal-free path component.

### Fixed

- `list_agents` no longer silently drops local agents when the registered slice
  already fills `limit` (previously truncate-then-merge starved them, including
  in the `dispatch_to_agent` routing path).
- Package-path encoding is forward-computed and exact-matched, so agent names
  containing dashes (`video-editor-agent`) resolve correctly — the existing
  `decode_project_path` is dash-lossy and must not be inverted.
- Hub resolution is `.resolve()`-d (symlinked `AGENTIHUB_DIR` no longer yields a
  permanent false `idle`) and memoized per process.

### Companion change (agentihub)

- All 8 agent packages declare A2A domain capabilities in
  `agents/*/package/command.yml`, grounded in each package's own CLAUDE.md.

## [0.2.1] - 2026-02-23

### Added
- **Phase 5 — Knowledge Catalog** with 5 new MCP tools:
  - `list_memory_files` — List memory files across projects
  - `get_memory_file` — Read a specific memory file
  - `list_plans` — List plans sorted by recency
  - `get_plan` — Read a plan by codename (with optional agent subplans)
  - `search_history` — Search the global prompt history
- New `catalog.py` module for memory, plans, and history operations
- `.dockerignore` for smaller Docker image builds
- `CHANGELOG.md` (this file)
- `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1)
- `SECURITY.md` with vulnerability reporting instructions
- GitHub issue templates (bug report, feature request)
- GitHub pull request template
- Dependabot configuration for pip and GitHub Actions

### Changed
- Total MCP tools increased from 11 to 16
- Unit test count increased from 452+ to 573+
- Updated all documentation references to reflect 16 tools and 573+ tests

### Fixed
- CI integration test dataset loading (switched to curl tarball)
- `get_plan` test assertion for flat response format
- Session count assertions for combined test data

## [0.2.0] - 2026-02-01

### Added
- **Phase 1 — Foundation** with 6 MCP tools:
  - `list_sessions`, `get_session`, `get_session_segment`, `get_session_actions`, `search_sessions`, `collect_now`
- **Phase 2 — Semantic Search** with 2 MCP tools:
  - `search_semantic`, `generate_summary`
- **Phase 3 — SSE/HTTP Transport** with API key and OAuth 2.1 authentication
- **Phase 4 — Dispatch** with 3 MCP tools:
  - `restore_session`, `dispatch_task`, `get_dispatch_job`
- Background collector daemon with incremental byte-offset parsing
- Redis + filesystem fallback pattern for all stateful operations
- Docker Compose deployment with Redis and PostgreSQL (pgvector)
- Cloudflare Tunnel support for remote access
- Dispatch bridge for Docker-to-host Claude CLI delegation
- CLI tool (`agentibridge status`, `agentibridge connect`, `agentibridge help`)
- Comprehensive documentation (architecture, deployment, reference)
- 452+ unit tests, stress tests, integration tests, E2E smoke tests
- GitHub Actions CI/CD (test, build, publish, release)
- PyPI package publishing

[0.7.0]: https://github.com/The-Cloud-Clockwork/agentibridge/compare/v0.6.0...v0.7.0
[0.2.1]: https://github.com/The-Cloud-Clockwork/agentibridge/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/The-Cloud-Clockwork/agentibridge/releases/tag/v0.2.0
