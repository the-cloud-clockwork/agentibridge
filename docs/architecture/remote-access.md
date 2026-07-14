---
title: Remote Access
nav_order: 2
parent: Architecture
---

# Remote Access (SSE Transport)

AgentiBridge can be accessed remotely via SSE (Server-Sent Events) over HTTP, allowing external clients like claude.ai, mobile apps, or other API consumers to query session transcripts without local filesystem access.

## Architecture

```
+------------------+     SSE/HTTP      +--------------------------+
|  claude.ai       | ----------------->|  AgentiBridge          |
|  Mobile app      |   X-API-Key auth  |  SSE transport (:8100)   |
|  API client      | <---------------- |                          |
+------------------+     Events        |  All 33 MCP tools        |
                                       |  + Redis backend         |
                                       +--------------------------+
```

## Transport Modes

AgentiBridge supports two transport modes:

| Mode | Default | Use Case |
|------|---------|----------|
| `stdio` | Yes | Local MCP client (Claude Code CLI) |
| `sse` | No | Remote HTTP clients |

Transport is selected via the `AGENTIBRIDGE_TRANSPORT` environment variable.

## Components

### `agentibridge/transport.py`

Provides SSE/HTTP transport with API key and OAuth authentication.

**Key components:**

| Class / Function | Description |
|----------|-------------|
| `run_sse_server(mcp)` | Build ASGI stack and start uvicorn |
| `APIKeyAuthMiddleware` | Validates `X-API-Key` header against `AGENTIBRIDGE_API_KEYS` |
| `OAuthCompatAuthMiddleware` | Dual auth: OAuth Bearer tokens + API keys |

### HTTP Endpoints

| Path | Auth | Description |
|------|------|-------------|
| `/health` | Public | Health check — always returns `{"status": "ok"}` |
| `/mcp` | Required | Streamable HTTP transport (preferred for new clients) |
| `/sse` | Required | Legacy Server-Sent Events transport |
| `/.well-known/oauth-authorization-server` | Public | OAuth metadata (only when OAuth enabled) |
| `/authorize`, `/token`, `/register`, `/revoke` | Public | OAuth 2.1 endpoints (only when OAuth enabled) |

### Authentication — API Keys

When `AGENTIBRIDGE_API_KEYS` is set, all requests to `/mcp` and `/sse` must include a valid key:

- **Header**: `X-API-Key: your-key`
- **Query param**: `?api_key=your-key`

When no keys are configured, auth is disabled (open access).

### Authentication — OAuth 2.1 (Optional)

Set `OAUTH_ISSUER_URL` to enable an in-memory OAuth 2.1 authorization server. This is required by some clients (e.g., claude.ai) that use the MCP OAuth flow. API key auth continues to work alongside OAuth — both methods are active simultaneously.

Uncomment and set these in `~/.agentibridge/agentibridge.env`:

```bash
OAUTH_ISSUER_URL=https://bridge.yourdomain.com   # must be your actual public hostname
OAUTH_CLIENT_ID=my-bridge-client                  # locks down dynamic registration
OAUTH_CLIENT_SECRET=generate-a-strong-secret-here
OAUTH_ALLOWED_REDIRECT_URIS=https://claude.ai/api/mcp/auth_callback
OAUTH_ALLOWED_SCOPES=claudeai
```

After changing OAuth config, you must recreate the containers (not just restart):

```bash
agentibridge stop    # tears down containers
agentibridge install     # recreates with new env vars
```

> **Why not `agentibridge restart`?** Docker Compose `restart` only restarts existing containers — environment variables are baked in at creation time and are not reloaded. You must `stop` + `run` to pick up any `agentibridge.env` changes.

Verify the OAuth discovery endpoint returns your actual hostname:

```bash
curl -s https://bridge.yourdomain.com/.well-known/oauth-authorization-server | head
```

See [Configuration Reference](../reference/configuration.md) for all OAuth variables.

### Transport Selection in `server.py`

```python
transport = os.getenv("AGENTIBRIDGE_TRANSPORT", "stdio")
if transport == "sse":
    from agentibridge.transport import run_sse_server
    run_sse_server(mcp)
else:
    mcp.run()  # stdio (default)
```

## Configuration

```bash
# Transport mode
AGENTIBRIDGE_TRANSPORT=stdio     # "stdio" (default) or "sse"

# SSE port
AGENTIBRIDGE_PORT=8100           # HTTP port for SSE transport

# API key auth (comma-separated, empty = no auth)
AGENTIBRIDGE_API_KEYS=key1,key2

# OAuth 2.1 (optional)
OAUTH_ISSUER_URL=https://bridge.example.com
```

## Remote Client Configuration

### Streamable HTTP (Preferred)

```json
{
  "mcpServers": {
    "agentibridge": {
      "url": "http://your-host:8100/mcp",
      "headers": {
        "X-API-Key": "your-api-key"
      }
    }
  }
}
```

### Legacy SSE

```json
{
  "mcpServers": {
    "agentibridge": {
      "url": "http://your-host:8100/sse",
      "headers": {
        "X-API-Key": "your-api-key"
      }
    }
  }
}
```

### Docker Compose

```bash
docker compose up --build -d
# Streamable HTTP: http://localhost:8100/mcp
# Legacy SSE:      http://localhost:8100/sse
# Health check:    http://localhost:8100/health
```

## Dependencies

- `starlette` — ASGI framework (included with `fastmcp` package)
- `uvicorn` — ASGI server
- `fastmcp` — FastMCP with streamable HTTP and SSE support

## Security Notes

- Always set `AGENTIBRIDGE_API_KEYS` when exposing SSE transport to a network
- Use HTTPS (reverse proxy) for production deployments
- API keys are checked against a simple comma-separated list (no hashing)
- OAuth state (tokens, clients) is in-memory only — lost on server restart
- Consider network-level restrictions (firewall, VPN) in addition to auth
