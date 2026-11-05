"""OAuth 2.1 Authorization Server Provider for AgentiBridge.

Implements the MCP OAuthAuthorizationServerProvider protocol with in-memory
storage. Enables claude.ai to connect via OAuth 2.1 (authorization_code + PKCE)
while API key auth continues working for CLI clients.

Activated when OAUTH_ISSUER_URL is set.
"""

import os
import secrets
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Optional
from urllib.parse import parse_qs, urlencode

import anyio.to_thread
import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from agentibridge.google_signin import (
    CALLBACK_PATH,
    CERTS_URL,
    START_PATH,
    SignInRefused,
    sign_in_url,
    verified_email,
)
from agentibridge.logging import log

# Token TTLs
_AUTH_CODE_TTL = 300  # 5 minutes
_ACCESS_TOKEN_TTL = 3600  # 1 hour
_REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days
_SIGN_IN_TTL = 600
_MAX_PENDING_SIGN_INS = 64
_MAX_CALLBACK_BODY = 16384
_BINDING_COOKIE_PREFIX = "agentibridge_signin_"


def _get_redirect_uris() -> list:
    """Load allowed redirect URIs from environment."""
    from pydantic import AnyUrl

    raw = os.getenv("OAUTH_ALLOWED_REDIRECT_URIS", "")
    if not raw.strip():
        return []
    return [AnyUrl(u.strip()) for u in raw.split(",") if u.strip()]


def binding_cookie(state: str) -> str:
    """The name of the cookie that binds a browser to the sign-in for ``state``."""
    return _BINDING_COOKIE_PREFIX + state


@dataclass(frozen=True)
class PendingSignIn:
    """An authorization request waiting for its Google sign-in."""

    client: OAuthClientInformationFull
    params: AuthorizationParams
    nonce: str
    expires_at: float
    binding: str
    started: bool = False


class BridgeOAuthProvider(OAuthAuthorizationServerProvider):
    """In-memory OAuth 2.1 provider for machine-to-machine bridge access.

    With a Google client ID, an authorization request waits for a Google
    sign-in from an allowed account; without one it is auto-approved.

    When client_id/client_secret are pre-configured, dynamic registration
    returns the pre-configured credentials (claude.ai requires working
    registration). When not set, dynamic registration is open.
    """

    def __init__(
        self,
        issuer_url: str,
        client_id: str = "",
        client_secret: str = "",
        google_client_id: str = "",
        allowed_emails: Iterable[str] = (),
    ) -> None:
        self.issuer_url = issuer_url
        self._locked = bool(client_id and client_secret)
        # In-memory stores
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        # Map access_token -> refresh_token for revocation pairing
        self._token_pairs: dict[str, str] = {}
        self.google_client_id = google_client_id
        self.allowed_emails = frozenset(email.strip().lower() for email in allowed_emails if email.strip())
        self._pending: dict[str, PendingSignIn] = {}
        self._pending_lock = threading.Lock()
        self._signing_keys: Optional[jwt.PyJWKClient] = None

        # Pre-register client if credentials are provided
        if self._locked:
            redirect_uris = _get_redirect_uris()
            if not redirect_uris:
                from pydantic import AnyUrl

                redirect_uris = [AnyUrl(issuer_url.rstrip("/") + "/callback")]

            # Scopes the client is allowed to request (space-separated)
            allowed_scopes = os.getenv("OAUTH_ALLOWED_SCOPES", "").strip() or None

            self._clients[client_id] = OAuthClientInformationFull(
                client_id=client_id,
                client_secret=client_secret,
                client_id_issued_at=int(time.time()),
                redirect_uris=redirect_uris,
                scope=allowed_scopes,
                token_endpoint_auth_method="client_secret_post",
            )
            log("OAuth pre-configured client loaded", {"client_id": client_id})

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        client = self._clients.get(client_id)
        log("OAuth get_client", {"client_id": client_id, "found": client is not None})
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        log(
            "OAuth register_client",
            {
                "locked": self._locked,
                "client_name": client_info.client_name,
                "redirect_uris": [str(u) for u in (client_info.redirect_uris or [])],
            },
        )
        if self._locked:
            pre = next(iter(self._clients.values()))
            client_info.client_id = pre.client_id
            client_info.client_secret = pre.client_secret
            client_info.client_id_issued_at = pre.client_id_issued_at
            client_info.redirect_uris = list(pre.redirect_uris or [])
            log(
                "OAuth register_client (locked: returning pre-configured)",
                {
                    "client_id": pre.client_id,
                    "redirect_uris": [str(u) for u in client_info.redirect_uris],
                },
            )
            return

        client_id = secrets.token_urlsafe(16)
        client_info.client_id = client_id
        client_info.client_id_issued_at = int(time.time())
        self._clients[client_id] = client_info
        log("OAuth client registered", {"client_id": client_id, "client_name": client_info.client_name})

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        log(
            "OAuth authorize",
            {
                "client_id": client.client_id,
                "scopes": params.scopes,
                "redirect_uri": str(params.redirect_uri),
                "resource": str(params.resource) if params.resource else None,
                "state": params.state[:16] + "..." if params.state and len(params.state) > 16 else params.state,
            },
        )
        if not client.client_id:
            raise AuthorizeError(error="invalid_request", error_description="Client has no client_id")
        if self.google_client_id:
            return self._start_google_sign_in(client, params)
        return self._issue_code(client, params)

    def _issue_code(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Store a fresh authorization code and return the client redirect that carries it."""
        code = secrets.token_urlsafe(32)
        now = time.time()

        auth_code = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=now + _AUTH_CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self._auth_codes[code] = auth_code

        log("OAuth authorize code issued", {"client_id": client.client_id, "code": code[:8] + "..."})

        return construct_redirect_uri(
            str(params.redirect_uri),
            code=code,
            state=params.state,
        )

    def _start_google_sign_in(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request under a fresh state and return the bridge URL that starts its Google sign-in."""
        now = time.time()
        state = secrets.token_urlsafe(32)
        entry = PendingSignIn(client, params, secrets.token_urlsafe(32), now + _SIGN_IN_TTL, secrets.token_urlsafe(32))
        with self._pending_lock:
            for key in [key for key, pending in self._pending.items() if pending.expires_at <= now]:
                del self._pending[key]
            if len(self._pending) >= _MAX_PENDING_SIGN_INS:
                raise AuthorizeError(error="temporarily_unavailable", error_description="too many sign-ins in progress")
            self._pending[state] = entry
        log("OAuth Google sign-in started", {"client_id": client.client_id})
        return f"{self.issuer_url.rstrip('/')}{START_PATH}?{urlencode({'state': state})}"

    def begin_google_sign_in(self, state: str) -> tuple[str, str]:
        """The Google sign-in URL and the browser binding for ``state``, handed out once."""
        with self._pending_lock:
            pending = self._pending.get(state)
            if pending is None or pending.expires_at < time.time() or pending.started:
                raise SignInRefused("sign-in request unknown, expired or already started")
            self._pending[state] = replace(pending, started=True)
        redirect_uri = self.issuer_url.rstrip("/") + CALLBACK_PATH
        return sign_in_url(self.google_client_id, redirect_uri, state, pending.nonce), pending.binding

    def finish_google_sign_in(self, state: str, id_token: str, binding: str) -> str:
        """The client redirect with a fresh code, once the starting browser proves an allowed Google account."""
        with self._pending_lock:
            pending = self._pending.get(state)
            if pending is None or not pending.started or pending.expires_at < time.time():
                raise SignInRefused("sign-in request unknown or expired")
            if not secrets.compare_digest(binding.encode(), pending.binding.encode()):
                raise SignInRefused("sign-in was not started in this browser")
            del self._pending[state]
        if self._signing_keys is None:
            self._signing_keys = jwt.PyJWKClient(CERTS_URL)
        email = verified_email(id_token, self.google_client_id, pending.nonce, self.allowed_emails, self._signing_keys)
        log("OAuth Google sign-in accepted", {"email": email, "client_id": pending.client.client_id})
        return self._issue_code(pending.client, pending.params)

    async def google_start(self, request: Request) -> Response:
        """Bind this browser to its sign-in with a cookie, then send it to Google."""
        state = request.query_params.get("state", "")
        try:
            location, binding = self.begin_google_sign_in(state)
        except SignInRefused as exc:
            log("OAuth Google sign-in refused", {"error": str(exc)})
            return PlainTextResponse(f"Sign-in refused: {exc}", status_code=403)
        response = RedirectResponse(location, status_code=302)
        response.set_cookie(
            binding_cookie(state),
            binding,
            max_age=_SIGN_IN_TTL,
            path=CALLBACK_PATH,
            secure=True,
            httponly=True,
            samesite="none",
        )
        return response

    async def google_callback(self, request: Request) -> Response:
        """The endpoint Google posts the ID token to."""
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > _MAX_CALLBACK_BODY:
                return PlainTextResponse("Sign-in refused: request body too large", status_code=413)
        form = parse_qs(bytes(body).decode("utf-8", errors="replace"))
        state = form.get("state", [""])[0]
        binding = request.cookies.get(binding_cookie(state), "")
        try:
            location = await anyio.to_thread.run_sync(
                self.finish_google_sign_in, state, form.get("id_token", [""])[0], binding
            )
        except SignInRefused as exc:
            log("OAuth Google sign-in refused", {"error": str(exc)})
            return PlainTextResponse(f"Sign-in refused: {exc}", status_code=403)
        response = RedirectResponse(location, status_code=302)
        response.delete_cookie(binding_cookie(state), path=CALLBACK_PATH, secure=True, httponly=True, samesite="none")
        return response

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> Optional[AuthorizationCode]:
        auth_code = self._auth_codes.get(authorization_code)
        if auth_code is None:
            return None
        if auth_code.client_id != client.client_id:
            return None
        if time.time() > auth_code.expires_at:
            self._auth_codes.pop(authorization_code, None)
            return None
        return auth_code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # Single-use: delete the code
        self._auth_codes.pop(authorization_code.code, None)

        now = int(time.time())
        access_token_str = secrets.token_urlsafe(32)
        refresh_token_str = secrets.token_urlsafe(32)

        self._access_tokens[access_token_str] = AccessToken(
            token=access_token_str,
            client_id=authorization_code.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + _ACCESS_TOKEN_TTL,
            resource=authorization_code.resource,
        )

        self._refresh_tokens[refresh_token_str] = RefreshToken(
            token=refresh_token_str,
            client_id=authorization_code.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + _REFRESH_TOKEN_TTL,
        )

        self._token_pairs[access_token_str] = refresh_token_str

        log(
            "OAuth code exchanged",
            {
                "client_id": authorization_code.client_id,
                "scopes": authorization_code.scopes,
                "access_token": access_token_str[:8] + "...",
                "resource": str(authorization_code.resource) if authorization_code.resource else None,
            },
        )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
            refresh_token=refresh_token_str,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> Optional[RefreshToken]:
        rt = self._refresh_tokens.get(refresh_token)
        if rt is None:
            return None
        if rt.client_id != client.client_id:
            return None
        if rt.expires_at is not None and time.time() > rt.expires_at:
            self._refresh_tokens.pop(refresh_token, None)
            return None
        return rt

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: delete old refresh token
        self._refresh_tokens.pop(refresh_token.token, None)

        now = int(time.time())
        new_access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)

        effective_scopes = scopes if scopes else refresh_token.scopes

        self._access_tokens[new_access] = AccessToken(
            token=new_access,
            client_id=refresh_token.client_id,
            scopes=effective_scopes,
            expires_at=now + _ACCESS_TOKEN_TTL,
        )

        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=refresh_token.client_id,
            scopes=effective_scopes,
            expires_at=now + _REFRESH_TOKEN_TTL,
        )

        self._token_pairs[new_access] = new_refresh

        log("OAuth refresh token rotated", {"client_id": refresh_token.client_id})

        return OAuthToken(
            access_token=new_access,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL,
            scope=" ".join(effective_scopes) if effective_scopes else None,
            refresh_token=new_refresh,
        )

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        token_prefix = token[:8] + "..." if len(token) > 8 else token

        # Check OAuth tokens first
        at = self._access_tokens.get(token)
        if at is not None:
            if at.expires_at is not None and time.time() > at.expires_at:
                self._access_tokens.pop(token, None)
                log("OAuth load_access_token", {"token": token_prefix, "result": "expired"})
                return None
            log(
                "OAuth load_access_token",
                {
                    "token": token_prefix,
                    "result": "valid",
                    "client_id": at.client_id,
                    "scopes": at.scopes,
                    "resource": str(at.resource) if at.resource else None,
                },
            )
            return at

        # Fallback: treat the token as an API key
        raw = os.getenv("AGENTIBRIDGE_API_KEYS", "")
        if raw.strip():
            api_keys = [k.strip() for k in raw.split(",") if k.strip()]
            if token in api_keys:
                log("OAuth load_access_token", {"token": token_prefix, "result": "api-key-match"})
                return AccessToken(
                    token=token,
                    client_id="api-key-client",
                    scopes=[],
                )

        log("OAuth load_access_token", {"token": token_prefix, "result": "not-found"})
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
            # Also revoke paired refresh token
            paired_rt = self._token_pairs.pop(token.token, None)
            if paired_rt:
                self._refresh_tokens.pop(paired_rt, None)
        elif isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)
            # Find and revoke paired access token
            for at_str, rt_str in list(self._token_pairs.items()):
                if rt_str == token.token:
                    self._access_tokens.pop(at_str, None)
                    self._token_pairs.pop(at_str, None)
                    break

        log("OAuth token revoked", {"token_type": type(token).__name__})
