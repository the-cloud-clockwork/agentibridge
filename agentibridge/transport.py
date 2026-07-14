"""HTTP transport with API key authentication for remote access.

Enables the AgentiBridge MCP server to be accessed remotely via
streamable HTTP (/mcp endpoint), with API key validation.
Legacy SSE transport (/sse) is also supported for backward compatibility.

Usage:
    AGENTIBRIDGE_TRANSPORT=sse AGENTIBRIDGE_PORT=8100 python -m agentibridge

Remote clients connect via:
    {"url": "http://host:8100/mcp", "headers": {"X-API-Key": "your-key"}}

Environment:
    AGENTIBRIDGE_TRANSPORT  — "stdio" (default) or "sse"
    AGENTIBRIDGE_HOST       — Bind address (default: 127.0.0.1)
    AGENTIBRIDGE_PORT       — HTTP port (default: 8100)
    AGENTIBRIDGE_API_KEYS   — Comma-separated valid API keys (empty = no auth)
"""

import json
import os
import sys
from typing import List, Optional

from agentibridge.logging import log


# =============================================================================
# API KEY AUTH
# =============================================================================


def _get_api_keys() -> List[str]:
    """Load valid API keys from environment."""
    raw = os.getenv("AGENTIBRIDGE_API_KEYS", "")
    if not raw.strip():
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def validate_api_key(key: Optional[str]) -> bool:
    """Check if the provided key is valid.

    Returns True if:
    - No API keys configured (auth disabled)
    - Key matches one of the configured keys
    """
    valid_keys = _get_api_keys()
    if not valid_keys:
        return True  # No auth configured
    return key in valid_keys


# =============================================================================
# ASGI AUTH MIDDLEWARE
# =============================================================================

# Paths that bypass authentication.
_PUBLIC_PATHS = frozenset({"/health"})

# OAuth endpoint paths that must be publicly accessible.
_OAUTH_PUBLIC_PATHS = frozenset({"/authorize", "/token", "/register", "/revoke"})


def _is_oauth_public_path(path: str) -> bool:
    """Check if a path should bypass auth for OAuth protocol endpoints."""
    return path in _OAUTH_PUBLIC_PATHS or path.startswith("/.well-known/")


class APIKeyAuthMiddleware:
    """ASGI middleware that validates X-API-Key header or api_key query param.

    Uses raw ASGI (not BaseHTTPMiddleware) so it correctly intercepts
    both regular HTTP requests and long-lived SSE connections.
    """

    def __init__(self, app):
        self.app = app
        self.api_keys = _get_api_keys()
        self.auth_enabled = len(self.api_keys) > 0

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Skip auth for public paths
        if path in _PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if self.auth_enabled:
            # Extract API key from headers
            key = None
            for header_name, header_value in scope.get("headers", []):
                if header_name.lower() == b"x-api-key":
                    key = header_value.decode("utf-8")
                    break

            # Fallback: check query string
            if key is None:
                qs = scope.get("query_string", b"").decode("utf-8")
                for param in qs.split("&"):
                    if param.startswith("api_key="):
                        key = param[8:]
                        break

            if not validate_api_key(key):
                log("SSE auth rejected", {"path": path})
                body = json.dumps({"error": "Invalid or missing API key"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"content-length", str(len(body)).encode()],
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return

        await self.app(scope, receive, send)


class OAuthCompatAuthMiddleware:
    """ASGI middleware for dual auth: OAuth Bearer tokens + API keys.

    Used when OAuth is enabled. Routes:
    - /health, OAuth endpoints, /.well-known/* → pass through (public)
    - /mcp + X-API-Key → convert to Authorization: Bearer header, pass to FastMCP
    - /mcp + Authorization: Bearer → pass through (FastMCP validates)
    - /mcp + nothing → pass through (FastMCP returns 401)
    - /sse, /messages + API key → validate with API key auth
    - /sse, /messages + no key → reject 401
    """

    def __init__(self, app):
        self.app = app
        self.api_keys = _get_api_keys()
        self.auth_enabled = len(self.api_keys) > 0

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Public paths: health + OAuth protocol endpoints
        if path in _PUBLIC_PATHS or _is_oauth_public_path(path):
            await self.app(scope, receive, send)
            return

        # /mcp path: handled by FastMCP's built-in Bearer auth
        if path.startswith("/mcp"):
            # Check for X-API-Key header and convert to Bearer for FastMCP
            headers = list(scope.get("headers", []))
            api_key = None
            has_auth_header = False

            for header_name, header_value in headers:
                name_lower = header_name.lower() if isinstance(header_name, bytes) else header_name
                if name_lower == b"x-api-key":
                    api_key = header_value.decode("utf-8") if isinstance(header_value, bytes) else header_value
                if name_lower == b"authorization":
                    has_auth_header = True

            log(
                "oauth-middleware",
                {
                    "path": path,
                    "has_bearer": has_auth_header,
                    "has_api_key": bool(api_key),
                    "method": scope.get("method", "?"),
                },
            )

            if api_key and not has_auth_header:
                # Convert API key to Bearer token so FastMCP's auth can validate it
                new_headers = [h for h in headers if h[0].lower() != b"x-api-key"]
                new_headers.append([b"authorization", f"Bearer {api_key}".encode()])
                scope = {**scope, "headers": new_headers}

            await self.app(scope, receive, send)
            return

        # SSE/messages paths: use API key auth (existing behavior)
        if self.auth_enabled:
            key = None
            for header_name, header_value in scope.get("headers", []):
                if header_name.lower() == b"x-api-key":
                    key = header_value.decode("utf-8")
                    break

            if key is None:
                qs = scope.get("query_string", b"").decode("utf-8")
                for param in qs.split("&"):
                    if param.startswith("api_key="):
                        key = param[8:]
                        break

            if not validate_api_key(key):
                log("SSE auth rejected", {"path": path})
                body = json.dumps({"error": "Invalid or missing API key"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"content-length", str(len(body)).encode()],
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return

        await self.app(scope, receive, send)


# =============================================================================
# HEALTH ENDPOINT
# =============================================================================


async def _health_endpoint(scope, receive, send):
    """Lightweight /health ASGI endpoint."""
    body = json.dumps({"status": "ok", "service": "agentibridge"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class HealthRouter:
    """ASGI app that routes /health to a handler, everything else to inner app."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/health":
            await _health_endpoint(scope, receive, send)
            return
        await self.app(scope, receive, send)


# =============================================================================
# AGENT REGISTRY REST ENDPOINTS
# =============================================================================


async def _read_json_body(receive) -> dict:
    """Read and parse JSON body from ASGI receive."""
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    if not body:
        return {}
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"Invalid JSON body: {e}") from e


async def _json_response(send, data: dict, status: int = 200):
    """Send a JSON response via ASGI."""
    body = json.dumps(data).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _handle_agents_request(scope, receive, send):
    """Handle /agents/* REST requests for service-to-service A2A calls."""
    from agentibridge import registry

    path = scope.get("path", "")
    method = scope.get("method", "GET")

    try:
        # POST /agents/register
        if path == "/agents/register" and method == "POST":
            body = await _read_json_body(receive)
            result = registry.register_agent(
                agent_id=body.get("agent_id", ""),
                agent_name=body.get("agent_name", ""),
                agent_type=body.get("agent_type", ""),
                capabilities=body.get("capabilities", []),
                endpoint=body.get("endpoint", ""),
                metadata=body.get("metadata", {}),
                heartbeat_ttl=body.get("heartbeat_ttl", 300),
                transport=body.get("transport", "http"),
            )
            await _json_response(send, {"success": True, **result})
            return

        # POST /agents/dispatch — capability-based routing
        if path == "/agents/dispatch" and method == "POST":
            body = await _read_json_body(receive)
            result = await registry.route_by_capability(
                capability=body.get("capability", ""),
                task=body.get("task", ""),
                profile=body.get("profile", ""),
                repo_url=body.get("repo_url", ""),
                wait=body.get("wait", False),
                file_path=body.get("file_path", ""),
            )
            status = 503 if result.get("retry") else (200 if result.get("success") else 400)
            await _json_response(send, result, status)
            return

        # POST /agents/{agent_id}/run — direct dispatch
        if path.endswith("/run") and method == "POST":
            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                agent_id = parts[1]
                body = await _read_json_body(receive)
                result = await registry.route_to_agent(
                    agent_id=agent_id,
                    task=body.get("task", ""),
                    profile=body.get("profile", ""),
                    repo_url=body.get("repo_url", ""),
                    wait=body.get("wait", False),
                    file_path=body.get("file_path", ""),
                )
                status = 503 if result.get("retry") else (200 if result.get("success") else 400)
                await _json_response(send, result, status)
                return

        # POST /agents/{agent_id}/heartbeat
        if path.endswith("/heartbeat") and method == "POST":
            parts = path.strip("/").split("/")
            # agents / {agent_id} / heartbeat
            if len(parts) >= 3:
                agent_id = parts[1]
                body = await _read_json_body(receive)
                result = registry.heartbeat_agent(
                    agent_id=agent_id,
                    status=body.get("status", "online"),
                    metadata=body.get("metadata"),
                )
                await _json_response(send, {"success": True, **result})
                return

        # DELETE /agents/{agent_id}
        if method == "DELETE":
            parts = path.strip("/").split("/")
            if len(parts) == 2:
                agent_id = parts[1]
                result = registry.deregister_agent(agent_id)
                await _json_response(send, {"success": True, **result})
                return

        # GET /agents/{agent_id}
        if method == "GET" and path != "/agents" and path != "/agents/":
            parts = path.strip("/").split("/")
            if len(parts) == 2:
                agent_id = parts[1]
                agent = registry.get_agent(agent_id)
                if agent is None:
                    await _json_response(send, {"success": False, "error": "not found"}, 404)
                    return
                await _json_response(send, {"success": True, "agent": agent})
                return

        # GET /agents
        if method == "GET":
            qs = scope.get("query_string", b"").decode("utf-8")
            params: dict = {}
            for param in qs.split("&"):
                if "=" in param:
                    k, v = param.split("=", 1)
                    params[k] = v
            agents = registry.list_agents(
                agent_type=params.get("agent_type", ""),
                capability=params.get("capability", ""),
                status=params.get("status", ""),
                limit=int(params.get("limit", "50")),
            )
            await _json_response(send, {"success": True, "count": len(agents), "agents": agents})
            return

        await _json_response(send, {"error": "not found"}, 404)

    except Exception as e:
        log("agents REST error", {"path": path, "error": str(e)})
        await _json_response(send, {"success": False, "error": str(e)}, 500)


class AgentRegistryRouter:
    """ASGI app that routes /agents/* to REST handlers, everything else to inner app."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/agents"):
            await _handle_agents_request(scope, receive, send)
            return
        await self.app(scope, receive, send)


# =============================================================================
# CORS MIDDLEWARE (simple ASGI wrapper)
# =============================================================================


class CORSMiddleware:
    """Minimal CORS middleware that adds permissive headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Handle preflight
        method = None
        for header_name, header_value in scope.get("headers", []):
            if header_name.lower() == b"access-control-request-method":
                method = header_value
                break

        if scope.get("method") == "OPTIONS" and method is not None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        [b"access-control-allow-origin", b"*"],
                        [b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"],
                        [b"access-control-allow-headers", b"content-type, x-api-key, authorization"],
                        [b"access-control-max-age", b"86400"],
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        path = scope.get("path", "")
        method = scope.get("method", "?")

        if path != "/health":
            log("http-request", {"method": method, "path": path})

        # Wrap send to inject CORS headers on responses
        captured_status = None

        async def cors_send(message):
            nonlocal captured_status
            if message["type"] == "http.response.start":
                captured_status = message.get("status")
                headers = list(message.get("headers", []))
                headers.append([b"access-control-allow-origin", b"*"])
                headers.append([b"access-control-expose-headers", b"*"])
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, cors_send)

        if path != "/health":
            log("http-response", {"method": method, "path": path, "status": captured_status})


# =============================================================================
# ENTRYPOINT
# =============================================================================


def _build_app(mcp):
    """Build the ASGI app stack with both /mcp and /sse endpoints.

    Wraps FastMCP's apps with:
    1. CORS middleware (outermost)
    2. Auth middleware (OAuthCompat when OAuth enabled, APIKey otherwise)
    3. Health endpoint router
    4. Dual-transport router: /mcp (streamable HTTP) + /sse (legacy)

    When OAuth is enabled, OAuth protocol paths (/authorize, /token,
    /register, /revoke, /.well-known/*) are routed to the HTTP app
    alongside /mcp.
    """
    from contextlib import asynccontextmanager

    http_app = mcp.streamable_http_app()  # Starlette app with /mcp route + lifespan
    sse_app = mcp.sse_app()  # Starlette app with /sse, /messages

    oauth_enabled = mcp.settings.auth is not None

    # The streamable HTTP app needs its session manager lifespan started.
    # We call it directly, then route /mcp to the HTTP app and everything
    # else to the legacy SSE app.
    session_manager = mcp.session_manager

    @asynccontextmanager
    async def lifespan():
        async with session_manager.run():
            yield

    _lifespan_cm = None

    async def dual_transport(scope, receive, send):
        nonlocal _lifespan_cm
        if scope["type"] == "lifespan":
            # Start the streamable HTTP session manager on startup
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    _lifespan_cm = lifespan()
                    await _lifespan_cm.__aenter__()
                    await send({"type": "lifespan.startup.complete"})
                except Exception:
                    await send({"type": "lifespan.startup.failed"})
                    return
            message = await receive()
            if message["type"] == "lifespan.shutdown":
                if _lifespan_cm:
                    await _lifespan_cm.__aexit__(None, None, None)
                await send({"type": "lifespan.shutdown.complete"})
            return

        path = scope.get("path", "")

        # Route to HTTP app: /mcp + OAuth endpoints (when enabled)
        if path.startswith("/mcp"):
            await http_app(scope, receive, send)
        elif oauth_enabled and _is_oauth_public_path(path):
            await http_app(scope, receive, send)
        else:
            await sse_app(scope, receive, send)

    app = dual_transport

    app = AgentRegistryRouter(app)
    app = HealthRouter(app)
    if oauth_enabled:
        app = OAuthCompatAuthMiddleware(app)
    else:
        app = APIKeyAuthMiddleware(app)
    app = CORSMiddleware(app)
    return app


def run_sse_server(mcp) -> None:
    """Build the ASGI app stack and run with uvicorn.

    Serves both /mcp (streamable HTTP, preferred) and /sse (legacy)
    endpoints behind auth + CORS middleware.

    Args:
        mcp: The FastMCP server instance (with host/port already configured)
    """
    try:
        import uvicorn
    except ImportError as e:
        print(f"SSE transport requires uvicorn: {e}", file=sys.stderr)
        print("Install with: pip install uvicorn", file=sys.stderr)
        sys.exit(1)

    api_keys = _get_api_keys()
    oauth_enabled = mcp.settings.auth is not None

    if oauth_enabled:
        print("OAuth 2.1 auth enabled (API keys also accepted)", file=sys.stderr)
    if api_keys:
        print(f"API key auth enabled ({len(api_keys)} key(s))", file=sys.stderr)
    elif not oauth_enabled:
        print("WARNING: No API keys configured — endpoint is unauthenticated", file=sys.stderr)

    app = _build_app(mcp)

    host = mcp.settings.host
    port = mcp.settings.port

    print(f"MCP transport ready on {host}:{port}", file=sys.stderr)
    print(f"  Streamable HTTP: http://{host}:{port}/mcp", file=sys.stderr)
    print(f"  Legacy SSE:      http://{host}:{port}/sse", file=sys.stderr)
    print(f"  Agent registry:  http://{host}:{port}/agents", file=sys.stderr)
    print(f"  Health check:    http://{host}:{port}/health", file=sys.stderr)
    if oauth_enabled:
        print(f"  OAuth metadata:  http://{host}:{port}/.well-known/oauth-authorization-server", file=sys.stderr)

    uvicorn.run(app, host=host, port=port, log_level="info")
