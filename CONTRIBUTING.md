# Contributing to AgentiBridge

Thank you for your interest in contributing to AgentiBridge! This guide will help you get set up for development and testing.

## Development Setup

### Prerequisites

- Python 3.14
- Docker and Docker Compose (for integration tests)
- Redis (optional, for local development)
- PostgreSQL with pgvector (optional, for semantic search development)

### Installation

```bash
# Clone the repository
git clone https://github.com/The-Cloud-Clockwork/agentibridge.git
cd agentibridge

# Install with development dependencies
pip install -e ".[dev]"
```

### Running Locally

**stdio transport (local MCP):**
```bash
python -m agentibridge
```

**SSE transport (remote clients):**
```bash
AGENTIBRIDGE_TRANSPORT=sse python -m agentibridge
```

**Docker Compose (full stack with Redis + Postgres):**
```bash
docker compose up --build -d
```

## Testing

### Unit Tests

We have 573+ unit tests covering core functionality:

```bash
# Run all unit tests
pytest tests/unit -v -m unit

# With coverage report
pytest tests/unit -v -m unit --cov=agentibridge

# Run specific test file
pytest tests/unit/test_parser.py -v

# Run specific test
pytest tests/unit/test_parser.py::test_parse_transcript -v
```

### Integration Tests

Docker-based integration tests validate the full stack (app + Redis):

```bash
# Start test environment
python tests/integration/test_docker.py --start

# Run integration tests
python tests/integration/test_docker.py --test

# Stop test environment
python tests/integration/test_docker.py --stop

# Or run all steps in sequence
python tests/integration/test_docker.py
```

These tests:
- Spin up Docker containers (agentibridge + Redis + Postgres)
- Exercise all MCP tools end-to-end (Phase 1–4)
- Verify Redis caching and fallback behavior
- Clean up automatically on completion

### Stress Tests

Performance and reliability tests:

```bash
pytest tests/stress -v -m stress
```

Includes:
- Large transcript parsing (10,000+ entries)
- Concurrent request handling
- Memory leak detection
- Redis connection pooling

### E2E Smoke Tests

End-to-end tests that call Phase 1 MCP tools via the Claude CLI against a live bridge:

```bash
# Prerequisites:
# 1. Claude CLI installed and in PATH
# 2. ~/.mcp.json configured with agentibridge connection
# 3. AgentiBridge running (local or remote)

./tests/e2e/test_mcp_smoke.sh
```

These tests:
- Validate real MCP client → server integration
- Test Phase 1 tools: `list_sessions`, `get_session`, `get_session_actions`, `search_sessions`, `collect_now`
- Run on a daily schedule via GitHub Actions (`e2e-smoke.yml`)

## Code Quality

### Linting and Formatting

We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
# Check for linting issues
ruff check agentibridge/ tests/

# Auto-fix linting issues
ruff check --fix agentibridge/ tests/

# Check formatting
ruff format --check agentibridge/ tests/

# Auto-format code
ruff format agentibridge/ tests/
```

### Pre-commit Hooks

We recommend setting up pre-commit hooks:

```bash
# Install pre-commit
pip install pre-commit

# Install hooks
pre-commit install

# Run manually
pre-commit run --all-files
```

## CI/CD Workflows

Our GitHub Actions workflows automatically run on pull requests:

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `test.yml` | Push/PR to main | Unit tests (Python 3.14), lint (ruff), integration tests (Docker) |
| `build.yml` | Tag push (`v*`) or manual | Builds Docker image → GHCR |
| `docker-publish.yml` | Tag push (`v*`) or manual | Publishes Docker image → Docker Hub |
| `publish-pypi.yml` | Tag push (`v*`) or manual | Builds wheel + sdist → PyPI |
| `release.yml` | Manual only | Bumps version in pyproject.toml, commits, tags, pushes |
| `docs-audit.yml` | Manual only | Audits docs against source code, creates PR with fixes (Claude CLI) |
| `e2e-smoke.yml` | Daily + manual | Runs 6 MCP tool smoke tests via Claude CLI against live tunnel |
| `claude.yml` | Issue/PR comments | Claude Code integration for automated code review |

### Running CI Checks Locally

Before pushing, ensure your changes pass all CI checks:

```bash
# Lint
ruff check agentibridge/ tests/
ruff format --check agentibridge/ tests/

# Unit tests (both Python versions if available)
pytest tests/unit -v -m unit --cov=agentibridge

# Integration tests
python tests/integration/test_docker.py
```

## Project Structure

```
agentibridge/
├── agentibridge/           # Main package
│   ├── server.py          # FastMCP server with 33 tools
│   ├── parser.py          # JSONL transcript parser
│   ├── store.py           # SessionStore (Redis + file fallback)
│   ├── collector.py       # Background polling daemon
│   ├── transport.py       # SSE/HTTP transport + auth middleware
│   ├── oauth_provider.py  # OAuth 2.1 authorization server (opt-in)
│   ├── embeddings.py      # Semantic search (Phase 2)
│   ├── dispatch.py        # Session restore + dispatch (Phase 4)
│   ├── dispatch_bridge.py # Host-side HTTP bridge for Docker dispatch
│   ├── claude_runner.py   # Claude CLI subprocess wrapper
│   ├── llm_client.py      # OpenAI-compatible LLM client
│   ├── redis_client.py    # Redis helper
│   ├── pg_client.py       # Postgres + pgvector
│   ├── config.py          # Centralized env-var configuration
│   ├── catalog.py         # Knowledge catalog (Phase 5)
│   ├── cli.py             # CLI tool
│   └── logging.py         # Structured JSON logging
├── tests/
│   ├── unit/              # Unit tests (573+ tests)
│   ├── integration/       # Docker-based integration tests
│   ├── stress/            # Performance tests
│   └── e2e/               # End-to-end smoke tests
├── docs/                  # Documentation
├── automation/            # Setup scripts
├── docker-compose.yml     # Docker Compose configuration
└── pyproject.toml         # Python project config
```

## Making Changes

### Adding a New Feature

1. **Create an issue** describing the feature
2. **Create a branch** from `main`: `git checkout -b feature/my-feature`
3. **Implement** the feature:
   - Add code in `agentibridge/`
   - Add tests in `tests/unit/`
   - Update documentation in `docs/`
4. **Test**:
   ```bash
   pytest tests/unit -v -m unit
   ruff check agentibridge/ tests/
   ruff format agentibridge/ tests/
   ```
5. **Commit** with a descriptive message:
   ```bash
   git add .
   git commit -m "feat: add new feature X"
   ```
6. **Push** and create a pull request:
   ```bash
   git push origin feature/my-feature
   ```

### Adding a New MCP Tool

1. **Add handler in `server.py`**:
   ```python
   @mcp.tool()
   async def my_new_tool(arg: str) -> dict:
       """Tool description for MCP registry."""
       result = await store.do_something(arg)
       return {"result": result}
   ```

2. **Add business logic in `store.py`** (or appropriate module)

3. **Add tests in `tests/unit/test_server.py`**:
   ```python
   def test_my_new_tool():
       result = await server.my_new_tool("test-arg")
       assert result["result"] == expected_value
   ```

4. **Update documentation**:
   - Add tool to README.md MCP Tools table
   - Add usage examples to relevant docs

### Fixing a Bug

1. **Create an issue** with reproduction steps
2. **Create a branch**: `git checkout -b fix/bug-description`
3. **Add a failing test** that reproduces the bug
4. **Fix the bug**
5. **Verify** the test now passes
6. **Follow standard commit/push process**

## Coding Standards

### Python Style

- Follow [PEP 8](https://peps.python.org/pep-0008/)
- Use type hints for all function signatures
- Maximum line length: 120 characters (ruff configured)
- Use docstrings for public functions and classes

### Example Function

```python
def parse_transcript(lines: list[str], filter_types: set[str] | None = None) -> dict:
    """Parse a Claude Code transcript from JSONL lines.

    Args:
        lines: List of JSONL strings (one entry per line)
        filter_types: Optional set of entry types to include (default: all indexed types)

    Returns:
        Dictionary with keys:
        - entries: List of parsed entry dicts
        - tool_calls: Extracted tool usage statistics
        - stats: Session statistics

    Raises:
        TranscriptParseError: If JSONL is malformed
    """
    # Implementation...
```

### Commit Messages

We follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat: add new tool X` — New feature
- `fix: correct parsing bug` — Bug fix
- `docs: update README` — Documentation only
- `test: add unit tests for Y` — Test additions
- `refactor: simplify parser logic` — Code refactoring
- `chore: update dependencies` — Maintenance tasks

## Automation Scripts

| Script | Purpose | Usage |
|--------|---------|-------|
| `automation/cloudfared.sh` | Idempotent Cloudflare Tunnel setup | `./automation/cloudfared.sh` |
| `automation/compose.sh` | Interactive Docker Compose manager with dispatch bridge support | `./automation/compose.sh` |

The Cloudflare setup script:
- Installs `cloudflared` (if not present)
- Authenticates with Cloudflare
- Creates a tunnel (if not exists)
- Configures DNS routing
- Writes `~/.cloudflared/config.yml`
- Optionally installs systemd service

It's **idempotent** — safe to re-run. All steps check for existing state and skip if already configured.

## Documentation

### Adding Documentation

1. **Architecture docs**: `docs/architecture/` — Deep dives into internal systems
2. **Reference docs**: `docs/reference/` — Configuration, API reference
3. **Deployment docs**: `docs/deployment/` — Production setup guides
4. **Getting started**: `docs/getting-started/` — User-facing tutorials

### Documentation Style

- Use clear, concise language
- Include code examples for all features
- Add command output examples where helpful
- Link to related docs for context

## Getting Help

- **GitHub Issues**: For bug reports and feature requests
- **GitHub Discussions**: For questions and general discussion
- **Pull Requests**: For code contributions

## License

By contributing to AgentiBridge, you agree that your contributions will be licensed under the MIT License.
