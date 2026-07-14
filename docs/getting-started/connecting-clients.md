---
title: Connecting Clients
nav_order: 1
parent: Getting Started
---

# Connecting Clients

Step-by-step instructions for connecting various AI clients to AgentiBridge.

## Prerequisites

- AgentiBridge running with SSE transport (`AGENTIBRIDGE_TRANSPORT=sse`)
- Server accessible at `http://HOST:PORT` (default: `http://localhost:8100`)
- API key configured if using authentication (`AGENTIBRIDGE_API_KEYS`)

Quick check:
```bash
curl http://localhost:8100/health
# Should return: {"status": "ok", "service": "agentibridge"}
```

## Claude Code CLI

Add to `~/.mcp.json` (or project-level `.mcp.json`):

```json
{
  "mcpServers": {
    "agentibridge": {
      "url": "http://localhost:8100/sse",
      "headers": {
        "X-API-Key": "your-api-key"
      }
    }
  }
}
```

Verify:
```bash
claude --mcp-debug
# Should show "agentibridge" in the connected servers list
```

### Available Tools in Claude Code

Once connected, Claude Code can use all 33 tools:
- "List my recent sessions" → `list_sessions`
- "What did I work on yesterday?" → `list_sessions` with `since_hours=24`
- "Search for Docker setup sessions" → `search_sessions`
- "Show me session abc-123" → `get_session`
- "What tools did I use?" → `get_session_actions`
- "Find sessions about authentication" → `search_semantic`
- "Summarize that session" → `generate_summary`
- "Continue from session abc-123" → `restore_session`
- "Run this task in the background" → `dispatch_task`
- "Did that job finish?" → `get_dispatch_job`
- "Which local agents can do cost-analysis?" → `find_agents`
- "Ask the finops agent to summarize last month's spend" → `dispatch_to_agent`

## ChatGPT Custom GPT (Actions)

1. Go to **ChatGPT** → **Explore GPTs** → **Create a GPT**
2. In the **Configure** tab, click **Create new action**
3. Set:
   - **Authentication**: API Key
   - **API Key**: Your `AGENTIBRIDGE_API_KEYS` value
   - **Auth Type**: Custom Header
   - **Header Name**: `X-API-Key`
4. Import the OpenAPI schema from your server, or manually configure the SSE endpoint URL

### Example Action Schema

```yaml
openapi: 3.0.0
info:
  title: AgentiBridge
  version: 0.2.0
servers:
  - url: http://your-host:8100
paths:
  /health:
    get:
      operationId: healthCheck
      summary: Check service health
      responses:
        '200':
          description: OK
```

Note: ChatGPT Actions work best with REST endpoints. For full MCP integration, use the SSE endpoint with an MCP-compatible client.

## Claude.ai (OAuth 2.1)

Claude.ai requires **OAuth 2.1** to connect to remote MCP servers. AgentiBridge has a built-in OAuth authorization server.

**1. Enable OAuth** — add to `~/.agentibridge/.env`:

```bash
OAUTH_ISSUER_URL=https://bridge.yourdomain.com
OAUTH_CLIENT_ID=my-bridge-client
OAUTH_CLIENT_SECRET=generate-a-strong-secret-here
OAUTH_ALLOWED_REDIRECT_URIS=https://claude.ai/api/mcp/auth_callback
OAUTH_ALLOWED_SCOPES=claudeai
```

**2. Expose over HTTPS** (e.g., Cloudflare Tunnel):

```bash
agentibridge tunnel setup    # interactive wizard
```

**3. Add to claude.ai:**

Go to [claude.ai/settings/connectors](https://claude.ai/settings/connectors) → **Add Server** → enter:

```
https://bridge.yourdomain.com/mcp
```

Claude.ai will automatically discover OAuth at `/.well-known/oauth-authorization-server`, complete the PKCE flow, and store the token.

**4. Verify:**

```bash
curl https://bridge.yourdomain.com/.well-known/oauth-authorization-server
curl https://bridge.yourdomain.com/health
```

## Grok (xAI)

For Grok or other clients that support MCP:

1. Use the SSE endpoint: `http://your-host:8100/sse`
2. Set authentication header: `X-API-Key: your-api-key`

## Generic MCP Client

Any MCP client that supports the SSE transport can connect:

```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client(
    "http://localhost:8100/sse",
    headers={"X-API-Key": "your-key"},
) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()

        # List available tools
        tools = await session.list_tools()
        for tool in tools.tools:
            print(f"  {tool.name}: {tool.description}")

        # Call a tool
        result = await session.call_tool("list_sessions", {"limit": 5})
        print(result)
```

## Troubleshooting

### Connection refused
- Verify the server is running: `curl http://HOST:PORT/health`
- Check if the port is open: `ss -tlnp | grep 8100`
- If behind a firewall, ensure port 8100 is allowed

### 401 Unauthorized
- Check your API key matches one in `AGENTIBRIDGE_API_KEYS`
- Verify the header name is `X-API-Key` (case-insensitive)
- Try with `?api_key=your-key` as a query parameter

### SSE connection drops
- Check reverse proxy settings (buffering must be disabled)
- Increase proxy timeouts
- Verify the server isn't running out of memory

### No sessions found
- Run `agentibridge status` to check transcript directory
- Trigger collection: call the `collect_now` tool
- Verify `~/.claude/projects/` contains `.jsonl` files
