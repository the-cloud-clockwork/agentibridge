---
title: Cloudflare Tunnel
nav_order: 3
parent: Deployment
---

# Cloudflare Tunnel Setup

Expose AgentiBridge to the internet securely using Cloudflare Tunnel. No port forwarding, firewall changes, or public IP required.

```
┌──────────┐     ┌─────────────────┐     ┌─────────────┐     ┌────────────────┐
│  Remote  │────▶│  Cloudflare     │────▶│ cloudflared │────▶│ agentibridge   │
│  Client  │ TLS │  Edge Network   │     │ (container) │     │ :8100          │
└──────────┘     └─────────────────┘     └─────────────┘     └────────────────┘
```

## Prerequisite — bind beyond loopback

The dockerized `cloudflared` reaches the native server via
`host.docker.internal`, which a loopback-only bind cannot serve (every
tunneled request 502s). In `~/.agentibridge/agentibridge.env` set:

```bash
AGENTIBRIDGE_HOST=0.0.0.0
AGENTIBRIDGE_API_KEYS=your-secret-key   # never open the bind without auth
```

then restart the server. The default (`localhost`) is loopback-only on purpose.

## Quick Tunnel (Zero Config)

No Cloudflare account needed. Generates a temporary `*.trycloudflare.com` URL.

```bash
docker compose --profile tunnel up -d
```

Get the tunnel URL:

```bash
agentibridge tunnel
# or
docker logs agentibridge-tunnel
```

The URL looks like `https://random-words.trycloudflare.com`. It changes each time the container restarts.

### Connect a remote client

```json
{
  "mcpServers": {
    "agentibridge": {
      "url": "https://random-words.trycloudflare.com/sse"
    }
  }
}
```

## Named Tunnel (Persistent Hostname)

For a stable hostname that survives restarts. Uses the `tunnel-named` profile (a separate container from the quick-tunnel profile).

### 1. Create a Cloudflare Tunnel

1. Go to [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/)
2. Navigate to **Networks > Tunnels**
3. Click **Create a tunnel** > **Cloudflared**
4. Name it (e.g., `agentibridge`)
5. Copy the tunnel token

### 2. Configure the route

In the tunnel configuration, add a **Public Hostname**:

| Field | Value |
|-------|-------|
| Subdomain | `bridge` (or your choice) |
| Domain | `example.com` (your domain) |
| Service Type | `HTTP` |
| URL | `agentibridge:8100` |

### 3. Start with token

The tunnel token is **static** — you set it once and it works permanently across restarts, reboots, and redeployments. It only changes if you delete and recreate the tunnel in the Zero Trust dashboard.

```bash
CLOUDFLARE_TUNNEL_TOKEN=eyJh... docker compose --profile tunnel-named up -d
```

Or add to `~/.agentibridge/agentibridge.env` (recommended — set once, forget about it):

```bash
CLOUDFLARE_TUNNEL_TOKEN=eyJh...
```

Then:

```bash
docker compose --profile tunnel-named up -d
```

> **Note:** The named tunnel uses a different Docker profile (`tunnel-named`) than the quick tunnel (`tunnel`). Do not mix them.

### 4. Verify

```bash
curl https://bridge.example.com/health
# {"status": "ok", "service": "agentibridge"}
```

## Named Tunnel via CLI (Non-Docker)

If you run agentibridge directly on the host (not in Docker), you can create a named tunnel using the `cloudflared` CLI. This is ideal for exposing a stable URL to GitHub Actions CI.

### 1. Install cloudflared

```bash
# Debian/Ubuntu
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# macOS
brew install cloudflared
```

### 2. Authenticate

```bash
cloudflared tunnel login
```

This opens your browser to authorize cloudflared with your Cloudflare account.

### 3. Create the tunnel

```bash
cloudflared tunnel create agentibridge
```

Note the tunnel UUID printed in the output. Credentials are saved to `~/.cloudflared/<tunnel-id>.json`.

### 4. Add a DNS route

Pick a subdomain on any domain already in your Cloudflare account:

```bash
cloudflared tunnel route dns agentibridge mcp.yourdomain.com
```

This creates a CNAME record `mcp.yourdomain.com` -> `<tunnel-id>.cfargotunnel.com` automatically. No manual DNS editing needed.

### 5. Configure the tunnel

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <tunnel-id>
credentials-file: /home/<user>/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: mcp.yourdomain.com
    service: http://localhost:8100
  - service: http_status:404
```

The `ingress` list must end with a catch-all rule. Port `8100` is the default `AGENTIBRIDGE_PORT`.

### 6. Run the tunnel

```bash
# Foreground (for testing)
cloudflared tunnel run agentibridge

# As a systemd service (persistent across reboots)
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

### 7. Verify

```bash
curl -s https://mcp.yourdomain.com/health
# {"status": "ok", "service": "agentibridge"}
```

### 8. Use in GitHub Actions

Set these in your repo settings (Settings > Secrets and variables > Actions):

| Type | Name | Value |
|------|------|-------|
| Variable | `MCP_SERVER_URL` | `https://mcp.yourdomain.com/mcp` |
| Secret | `MCP_API_KEY` | Your `AGENTIBRIDGE_API_KEYS` value |

The `e2e-smoke.yml` workflow uses these to generate `.mcp.json` and run smoke tests against your tunnel.

---

## Allowing claude.ai and MCP Clients Through Cloudflare

When **claude.ai** (or any automated MCP client) connects to your AgentiBridge server through Cloudflare, Cloudflare's bot protection features may block the connection. These features treat automated clients as bots and return HTTP 403 or 1020 errors, which appear as MCP connection failures in claude.ai.

### Symptoms

- Claude.ai reports "could not connect to MCP server" even though `curl` works from your machine
- HTTP 403 / Cloudflare error 1020 in tunnel logs
- SSE connection established then immediately dropped

### Fix 1 — Disable Bot Fight Mode (simplest)

Bot Fight Mode is the most common culprit. Disable it for the hostname used by your MCP server:

1. Go to [Cloudflare Dashboard](https://dash.cloudflare.com/) → your domain → **Security > Bots**
2. Set **Bot Fight Mode** to **Off** (or switch to **Super Bot Fight Mode** with granular rules)

If you use **Super Bot Fight Mode**, configure it to allow "Verified bots" and add a custom rule to allow the MCP connection path:

1. Go to **Security > WAF > Custom Rules**
2. Create a rule:
   - **Field:** URI Path — **Operator:** starts with — **Value:** `/sse` (or `/mcp`)
   - **Action:** Skip → Skip all remaining custom rules
   - **Also skip:** Bot Fight Mode managed rules

### Fix 2 — WAF Bypass Rule (recommended for production)

Create a WAF custom rule that skips Cloudflare's managed ruleset for MCP traffic:

1. Go to **Security > WAF > Custom Rules** → **Create rule**
2. Configure:
   ```
   (http.request.uri.path eq "/sse" or http.request.uri.path eq "/mcp")
   ```
3. Action: **Skip** → check **Skip all remaining custom rules** and **Skip managed rules**

This preserves protection for your other routes while letting MCP long-lived connections through.

### Fix 3 — Cloudflare Access Service Token (for LLM backend behind Access)

This is not an AgentiBridge feature per se — it covers a specific deployment pattern where your LLM proxy (LiteLLM, OpenRouter, etc.) is protected by Cloudflare Access Zero Trust. The project author uses this setup: all LLM endpoints are behind Cloudflare Access, so AgentiBridge needs service-token credentials to make outbound API requests to them. If your LLM backend is not behind Cloudflare Access, skip this section.

Set these in `~/.agentibridge/agentibridge.env`:

```bash
# Cloudflare Access service-token for the LLM backend
CF_ACCESS_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.access
CF_ACCESS_CLIENT_SECRET=yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
```

**Where to get these values:**
1. Go to [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/) → **Access > Service Auth > Service Tokens**
2. Click **Create Service Token**
3. Copy the **Client ID** → `CF_ACCESS_CLIENT_ID`
4. Copy the **Client Secret** → `CF_ACCESS_CLIENT_SECRET`
5. In your Access policy protecting the LLM backend, add a rule: **Service Token** is the token you created

> **Note:** These variables control *outbound* requests AgentiBridge makes to its LLM backend — not inbound connections from clients. They are unrelated to `CLOUDFLARE_TUNNEL_TOKEN`.

**Example `.env` with both tunnel and LLM Access:**

```bash
# Named tunnel for inbound MCP connections
CLOUDFLARE_TUNNEL_TOKEN=eyJh...

# Service token for outbound calls to LiteLLM behind Cloudflare Access
CF_ACCESS_CLIENT_ID=abc123.access
CF_ACCESS_CLIENT_SECRET=supersecretvalue

# LLM backend URL (behind Cloudflare Access)
LLM_API_BASE=https://llm.internal.example.com/v1
LLM_API_KEY=your-litellm-api-key
```

---

## Security Checklist

1. **Set API keys** — Always set `AGENTIBRIDGE_API_KEYS` when exposing to the internet:
   ```bash
   AGENTIBRIDGE_API_KEYS=my-secret-key-1,my-secret-key-2
   ```

2. **Allow MCP bots** — Disable Bot Fight Mode or add a WAF bypass for `/sse` and `/mcp` paths (see above) so that claude.ai and other MCP clients can connect.

3. **Use Cloudflare Access (optional)** — Add an Access policy in the Zero Trust dashboard for additional authentication (SSO, email OTP, etc.)

4. **TLS is automatic** — Cloudflare handles TLS termination at the edge. The connection between cloudflared and agentibridge stays internal to the Docker network.

5. **No ports exposed** — The tunnel does not require any inbound ports. All connections are outbound from cloudflared to Cloudflare's edge.

---

## Troubleshooting

### Tunnel URL not appearing

Quick tunnel URLs take a few seconds to register. Wait and retry:

```bash
sleep 10 && agentibridge tunnel
```

Or check raw logs:

```bash
docker logs agentibridge-tunnel 2>&1 | grep trycloudflare
```

### claude.ai says "could not connect" (HTTP 403 / 1020)

Cloudflare is blocking the connection. See [Allowing claude.ai and MCP Clients Through Cloudflare](#allowing-claudeai-and-mcp-clients-through-cloudflare) above.

### SSE connection drops

Cloudflare Tunnel handles long-lived connections well by default. If you experience drops:

- Ensure you're using `https://` (not `http://`) for the tunnel URL
- For named tunnels, set `noTLSVerify: true` in the tunnel config if the origin uses self-signed certs (not needed with the default Docker setup)
- Check that Bot Fight Mode is not terminating the long-lived SSE connection

### Container won't start

The cloudflared container waits for agentibridge to be healthy:

```bash
# Check agentibridge health
docker inspect -f '{{.State.Health.Status}}' agentibridge

# Check cloudflared logs
docker logs agentibridge-tunnel
```

### LLM requests failing with 403 when LLM backend is behind Cloudflare Access

Set `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` in `~/.agentibridge/.env`. See [Fix 3](#fix-3--cloudflare-access-service-token-for-llm-backend-behind-access) above.

### Stopping the tunnel

```bash
# Stop quick tunnel
docker compose --profile tunnel stop cloudflared

# Stop named tunnel
docker compose --profile tunnel-named stop cloudflared-named

# Stop everything
docker compose --profile tunnel down
docker compose --profile tunnel-named down
```

---

## How It Works

The `docker-compose.yml` includes two cloudflared services, each behind its own profile:

| Profile | Service | When to use |
|---------|---------|-------------|
| `tunnel` | `cloudflared` | Quick tunnel — temporary `*.trycloudflare.com` URL, no Cloudflare account |
| `tunnel-named` | `cloudflared-named` | Named tunnel — persistent hostname via Cloudflare Zero Trust |

`docker compose up` works normally without either — you only activate a tunnel when you explicitly use `--profile`.

The named-tunnel container detects its token automatically:
- **`CLOUDFLARE_TUNNEL_TOKEN` set**: Authenticates and routes traffic to your configured hostname
- **`CLOUDFLARE_TUNNEL_TOKEN` unset**: The `tunnel-named` profile will fail to start — use `--profile tunnel` instead for quick tunnels
