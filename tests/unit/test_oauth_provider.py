"""Unit tests for agentibridge.oauth_provider module."""

import time

import pytest
from pydantic import AnyUrl

from agentibridge.oauth_provider import (
    BridgeOAuthProvider,
    _ACCESS_TOKEN_TTL,
    _AUTH_CODE_TTL,
    _REFRESH_TOKEN_TTL,
)
from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
)
from mcp.shared.auth import OAuthClientInformationFull


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ISSUER = "https://bridge.example.com"
_REDIRECT_URI = "https://client.example.com/callback"


def _make_client_info(client_id=None, client_name="test-client"):
    """Create a minimal OAuthClientInformationFull for testing."""
    return OAuthClientInformationFull(
        redirect_uris=[AnyUrl(_REDIRECT_URI)],
        client_name=client_name,
        client_id=client_id,
    )


def _make_auth_params(state="test-state", code_challenge="challenge123", scopes=None, resource=None):
    """Create AuthorizationParams for testing."""
    return AuthorizationParams(
        state=state,
        scopes=scopes,
        code_challenge=code_challenge,
        redirect_uri=AnyUrl(_REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


# ============================================================================
# BridgeOAuthProvider — Client Management
# ============================================================================


@pytest.mark.unit
class TestClientManagement:
    """Tests for get_client and register_client."""

    @pytest.mark.asyncio
    async def test_get_client_unknown_returns_none(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        assert await provider.get_client("nonexistent") is None

    @pytest.mark.asyncio
    async def test_register_client_assigns_id(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info()
        assert client.client_id is None

        await provider.register_client(client)

        assert client.client_id is not None
        assert len(client.client_id) > 0

    @pytest.mark.asyncio
    async def test_register_client_sets_issued_at(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info()

        before = int(time.time())
        await provider.register_client(client)
        after = int(time.time())

        assert before <= client.client_id_issued_at <= after

    @pytest.mark.asyncio
    async def test_registered_client_is_retrievable(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info()
        await provider.register_client(client)

        retrieved = await provider.get_client(client.client_id)
        assert retrieved is not None
        assert retrieved.client_id == client.client_id
        assert retrieved.client_name == "test-client"

    @pytest.mark.asyncio
    async def test_register_multiple_clients_unique_ids(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        c1 = _make_client_info(client_name="client-1")
        c2 = _make_client_info(client_name="client-2")
        await provider.register_client(c1)
        await provider.register_client(c2)

        assert c1.client_id != c2.client_id


# ============================================================================
# BridgeOAuthProvider — Authorization
# ============================================================================


@pytest.mark.unit
class TestAuthorization:
    """Tests for authorize and load_authorization_code."""

    @pytest.mark.asyncio
    async def test_authorize_returns_redirect_with_code(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params(state="s1")
        redirect = await provider.authorize(client, params)

        assert redirect.startswith(_REDIRECT_URI)
        assert "code=" in redirect
        assert "state=s1" in redirect

    @pytest.mark.asyncio
    async def test_authorize_no_client_id_raises(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info()  # no client_id
        params = _make_auth_params()

        with pytest.raises(AuthorizeError):
            await provider.authorize(client, params)

    @pytest.mark.asyncio
    async def test_load_authorization_code_valid(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)

        # Extract code from redirect URL
        code = None
        for part in redirect.split("?")[1].split("&"):
            if part.startswith("code="):
                code = part[5:]
                break
        assert code is not None

        loaded = await provider.load_authorization_code(client, code)
        assert loaded is not None
        assert loaded.code == code
        assert loaded.client_id == "client-1"

    @pytest.mark.asyncio
    async def test_load_authorization_code_wrong_client(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client1 = _make_client_info(client_id="client-1")
        client2 = _make_client_info(client_id="client-2")
        provider._clients["client-1"] = client1
        provider._clients["client-2"] = client2

        params = _make_auth_params()
        redirect = await provider.authorize(client1, params)
        code = redirect.split("code=")[1].split("&")[0]

        # Try loading with wrong client
        loaded = await provider.load_authorization_code(client2, code)
        assert loaded is None

    @pytest.mark.asyncio
    async def test_load_authorization_code_expired(self, monkeypatch):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code = redirect.split("code=")[1].split("&")[0]

        # Manually expire the code
        provider._auth_codes[code].expires_at = time.time() - 1

        loaded = await provider.load_authorization_code(client, code)
        assert loaded is None

    @pytest.mark.asyncio
    async def test_load_authorization_code_unknown(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        loaded = await provider.load_authorization_code(client, "nonexistent-code")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_authorize_stores_code_challenge(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params(code_challenge="my-pkce-challenge")
        redirect = await provider.authorize(client, params)
        code = redirect.split("code=")[1].split("&")[0]

        loaded = await provider.load_authorization_code(client, code)
        assert loaded.code_challenge == "my-pkce-challenge"

    @pytest.mark.asyncio
    async def test_authorize_stores_resource(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params(resource="https://bridge.example.com/mcp")
        redirect = await provider.authorize(client, params)
        code = redirect.split("code=")[1].split("&")[0]

        loaded = await provider.load_authorization_code(client, code)
        assert loaded.resource == "https://bridge.example.com/mcp"


# ============================================================================
# BridgeOAuthProvider — Token Exchange
# ============================================================================


@pytest.mark.unit
class TestTokenExchange:
    """Tests for exchange_authorization_code."""

    @pytest.mark.asyncio
    async def test_exchange_returns_oauth_token(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params(scopes=["read", "write"])
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)

        token = await provider.exchange_authorization_code(client, auth_code)

        assert token.access_token
        assert token.refresh_token
        assert token.token_type == "Bearer"
        assert token.expires_in == _ACCESS_TOKEN_TTL
        assert "read" in token.scope
        assert "write" in token.scope

    @pytest.mark.asyncio
    async def test_exchange_code_is_single_use(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)

        await provider.exchange_authorization_code(client, auth_code)

        # Code should be consumed
        loaded_again = await provider.load_authorization_code(client, code_str)
        assert loaded_again is None

    @pytest.mark.asyncio
    async def test_exchange_creates_valid_access_token(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)

        token = await provider.exchange_authorization_code(client, auth_code)

        # Access token should be loadable
        at = await provider.load_access_token(token.access_token)
        assert at is not None
        assert at.client_id == "client-1"

    @pytest.mark.asyncio
    async def test_exchange_creates_valid_refresh_token(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)

        token = await provider.exchange_authorization_code(client, auth_code)

        # Refresh token should be loadable
        rt = await provider.load_refresh_token(client, token.refresh_token)
        assert rt is not None
        assert rt.client_id == "client-1"


# ============================================================================
# BridgeOAuthProvider — Refresh Token
# ============================================================================


@pytest.mark.unit
class TestRefreshToken:
    """Tests for load_refresh_token and exchange_refresh_token."""

    async def _get_tokens(self, provider, client):
        """Helper to do full authorize + exchange flow."""
        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)
        return await provider.exchange_authorization_code(client, auth_code)

    @pytest.mark.asyncio
    async def test_refresh_returns_new_tokens(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        token = await self._get_tokens(provider, client)
        old_rt = await provider.load_refresh_token(client, token.refresh_token)

        new_token = await provider.exchange_refresh_token(client, old_rt, [])

        assert new_token.access_token != token.access_token
        assert new_token.refresh_token != token.refresh_token
        assert new_token.token_type == "Bearer"

    @pytest.mark.asyncio
    async def test_refresh_rotates_old_token(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        token = await self._get_tokens(provider, client)
        old_rt_str = token.refresh_token
        old_rt = await provider.load_refresh_token(client, old_rt_str)

        await provider.exchange_refresh_token(client, old_rt, [])

        # Old refresh token should be gone
        assert await provider.load_refresh_token(client, old_rt_str) is None

    @pytest.mark.asyncio
    async def test_refresh_with_scopes(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        token = await self._get_tokens(provider, client)
        old_rt = await provider.load_refresh_token(client, token.refresh_token)

        new_token = await provider.exchange_refresh_token(client, old_rt, ["scope1"])

        assert new_token.scope == "scope1"

    @pytest.mark.asyncio
    async def test_load_refresh_token_wrong_client(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        c1 = _make_client_info(client_id="client-1")
        c2 = _make_client_info(client_id="client-2")
        provider._clients["client-1"] = c1
        provider._clients["client-2"] = c2

        token = await self._get_tokens(provider, c1)
        loaded = await provider.load_refresh_token(c2, token.refresh_token)
        assert loaded is None

    @pytest.mark.asyncio
    async def test_load_refresh_token_expired(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        token = await self._get_tokens(provider, client)

        # Manually expire
        provider._refresh_tokens[token.refresh_token].expires_at = int(time.time()) - 1

        loaded = await provider.load_refresh_token(client, token.refresh_token)
        assert loaded is None

    @pytest.mark.asyncio
    async def test_load_refresh_token_unknown(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        loaded = await provider.load_refresh_token(client, "nonexistent")
        assert loaded is None


# ============================================================================
# BridgeOAuthProvider — Access Token
# ============================================================================


@pytest.mark.unit
class TestAccessToken:
    """Tests for load_access_token."""

    @pytest.mark.asyncio
    async def test_load_valid_access_token(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)
        token = await provider.exchange_authorization_code(client, auth_code)

        at = await provider.load_access_token(token.access_token)
        assert at is not None
        assert at.token == token.access_token
        assert at.client_id == "client-1"

    @pytest.mark.asyncio
    async def test_load_expired_access_token(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)
        token = await provider.exchange_authorization_code(client, auth_code)

        # Expire it
        provider._access_tokens[token.access_token].expires_at = int(time.time()) - 1

        at = await provider.load_access_token(token.access_token)
        assert at is None

    @pytest.mark.asyncio
    async def test_load_unknown_access_token(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        at = await provider.load_access_token("nonexistent-token")
        assert at is None

    @pytest.mark.asyncio
    async def test_api_key_accepted_as_access_token(self, monkeypatch):
        monkeypatch.setenv("AGENTIBRIDGE_API_KEYS", "my-api-key,other-key")
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)

        at = await provider.load_access_token("my-api-key")
        assert at is not None
        assert at.token == "my-api-key"
        assert at.client_id == "api-key-client"

    @pytest.mark.asyncio
    async def test_invalid_api_key_not_accepted(self, monkeypatch):
        monkeypatch.setenv("AGENTIBRIDGE_API_KEYS", "my-api-key")
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)

        at = await provider.load_access_token("wrong-key")
        assert at is None

    @pytest.mark.asyncio
    async def test_no_api_keys_configured_returns_none(self, monkeypatch):
        monkeypatch.delenv("AGENTIBRIDGE_API_KEYS", raising=False)
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)

        at = await provider.load_access_token("some-token")
        assert at is None

    @pytest.mark.asyncio
    async def test_empty_api_keys_returns_none(self, monkeypatch):
        monkeypatch.setenv("AGENTIBRIDGE_API_KEYS", "")
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)

        at = await provider.load_access_token("some-token")
        assert at is None


# ============================================================================
# BridgeOAuthProvider — Revocation
# ============================================================================


@pytest.mark.unit
class TestRevocation:
    """Tests for revoke_token."""

    @pytest.mark.asyncio
    async def test_revoke_access_token(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)
        token = await provider.exchange_authorization_code(client, auth_code)

        at = await provider.load_access_token(token.access_token)
        await provider.revoke_token(at)

        assert await provider.load_access_token(token.access_token) is None

    @pytest.mark.asyncio
    async def test_revoke_access_token_also_revokes_refresh(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)
        token = await provider.exchange_authorization_code(client, auth_code)

        at = await provider.load_access_token(token.access_token)
        await provider.revoke_token(at)

        # Paired refresh token should also be gone
        assert await provider.load_refresh_token(client, token.refresh_token) is None

    @pytest.mark.asyncio
    async def test_revoke_refresh_token(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)
        token = await provider.exchange_authorization_code(client, auth_code)

        rt = await provider.load_refresh_token(client, token.refresh_token)
        await provider.revoke_token(rt)

        assert await provider.load_refresh_token(client, token.refresh_token) is None

    @pytest.mark.asyncio
    async def test_revoke_refresh_token_also_revokes_access(self):
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)
        token = await provider.exchange_authorization_code(client, auth_code)

        rt = await provider.load_refresh_token(client, token.refresh_token)
        await provider.revoke_token(rt)

        # Paired access token should also be gone
        assert await provider.load_access_token(token.access_token) is None


# ============================================================================
# BridgeOAuthProvider — TTL Constants
# ============================================================================


@pytest.mark.unit
class TestTTLConstants:
    """Verify token TTL constants are reasonable."""

    def test_auth_code_ttl(self):
        assert _AUTH_CODE_TTL == 300  # 5 minutes

    def test_access_token_ttl(self):
        assert _ACCESS_TOKEN_TTL == 3600  # 1 hour

    def test_refresh_token_ttl(self):
        assert _REFRESH_TOKEN_TTL == 30 * 24 * 3600  # 30 days


# ============================================================================
# BridgeOAuthProvider — Full Flow
# ============================================================================


@pytest.mark.unit
class TestFullFlow:
    """End-to-end OAuth flow tests."""

    @pytest.mark.asyncio
    async def test_register_authorize_exchange_use(self):
        """Full flow: register -> authorize -> exchange -> use token."""
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)

        # 1. Register client
        client = _make_client_info()
        await provider.register_client(client)
        assert client.client_id is not None

        # 2. Authorize
        params = _make_auth_params(scopes=["mcp"])
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]

        # 3. Exchange code for tokens
        auth_code = await provider.load_authorization_code(client, code_str)
        token = await provider.exchange_authorization_code(client, auth_code)

        # 4. Use access token
        at = await provider.load_access_token(token.access_token)
        assert at is not None
        assert at.client_id == client.client_id

    @pytest.mark.asyncio
    async def test_refresh_cycle(self):
        """Full flow with token refresh."""
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)

        client = _make_client_info()
        await provider.register_client(client)

        params = _make_auth_params()
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)
        token = await provider.exchange_authorization_code(client, auth_code)

        # Refresh
        rt = await provider.load_refresh_token(client, token.refresh_token)
        new_token = await provider.exchange_refresh_token(client, rt, [])

        # Old access token may still exist, but new one should work
        new_at = await provider.load_access_token(new_token.access_token)
        assert new_at is not None

        # New refresh token should work
        new_rt = await provider.load_refresh_token(client, new_token.refresh_token)
        assert new_rt is not None

    @pytest.mark.asyncio
    async def test_authorize_with_none_state(self):
        """Authorization with state=None should work."""
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params(state=None)
        redirect = await provider.authorize(client, params)

        assert "code=" in redirect
        # state param should not appear when None
        assert "state=" not in redirect

    @pytest.mark.asyncio
    async def test_exchange_with_empty_scopes(self):
        """Exchange code with empty scopes produces token with no scope."""
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info(client_id="client-1")
        provider._clients["client-1"] = client

        params = _make_auth_params(scopes=[])
        redirect = await provider.authorize(client, params)
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)

        token = await provider.exchange_authorization_code(client, auth_code)
        assert token.scope is None


# ============================================================================
# BridgeOAuthProvider — Pre-configured Client Credentials
# ============================================================================


@pytest.mark.unit
class TestPreConfiguredCredentials:
    """Tests for locked-down mode with OAUTH_CLIENT_ID + OAUTH_CLIENT_SECRET."""

    @pytest.mark.asyncio
    async def test_preconfigured_client_is_retrievable(self):
        """Pre-configured client can be looked up by client_id."""
        provider = BridgeOAuthProvider(
            issuer_url=_ISSUER,
            client_id="my-client",
            client_secret="my-secret",
        )
        client = await provider.get_client("my-client")
        assert client is not None
        assert client.client_id == "my-client"
        assert client.client_secret == "my-secret"

    @pytest.mark.asyncio
    async def test_preconfigured_unknown_client_returns_none(self):
        """Unknown client_id returns None even when pre-configured."""
        provider = BridgeOAuthProvider(
            issuer_url=_ISSUER,
            client_id="my-client",
            client_secret="my-secret",
        )
        assert await provider.get_client("other-client") is None

    @pytest.mark.asyncio
    async def test_dynamic_registration_returns_preconfigured_when_locked(self):
        """Dynamic registration returns pre-configured credentials when locked."""
        provider = BridgeOAuthProvider(
            issuer_url=_ISSUER,
            client_id="my-client",
            client_secret="my-secret",
        )
        new_client = _make_client_info()

        await provider.register_client(new_client)

        assert new_client.client_id == "my-client"
        assert new_client.client_secret == "my-secret"

        # Verify stored client is retrievable
        stored = await provider.get_client("my-client")
        assert stored is not None
        assert [str(uri) for uri in stored.redirect_uris] == [f"{_ISSUER}/callback"]
        assert [str(uri) for uri in new_client.redirect_uris] == [f"{_ISSUER}/callback"]

    @pytest.mark.asyncio
    async def test_dynamic_registration_works_when_unlocked(self):
        """Dynamic registration works when no credentials are pre-configured."""
        provider = BridgeOAuthProvider(issuer_url=_ISSUER)
        client = _make_client_info()
        await provider.register_client(client)
        assert client.client_id is not None

    @pytest.mark.asyncio
    async def test_locked_provider_flag(self):
        """_locked is True only when both client_id and secret are set."""
        unlocked = BridgeOAuthProvider(issuer_url=_ISSUER)
        assert unlocked._locked is False

        id_only = BridgeOAuthProvider(issuer_url=_ISSUER, client_id="x")
        assert id_only._locked is False

        secret_only = BridgeOAuthProvider(issuer_url=_ISSUER, client_secret="x")
        assert secret_only._locked is False

        locked = BridgeOAuthProvider(issuer_url=_ISSUER, client_id="x", client_secret="y")
        assert locked._locked is True

    @pytest.mark.asyncio
    async def test_preconfigured_full_oauth_flow(self):
        """Full OAuth flow works with a pre-configured client."""
        provider = BridgeOAuthProvider(
            issuer_url=_ISSUER,
            client_id="my-client",
            client_secret="my-secret",
        )
        client = await provider.get_client("my-client")
        assert client is not None

        # Authorize
        params = _make_auth_params(scopes=["mcp"])
        redirect = await provider.authorize(client, params)
        assert "code=" in redirect

        # Exchange
        code_str = redirect.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client, code_str)
        token = await provider.exchange_authorization_code(client, auth_code)

        assert token.access_token
        assert token.refresh_token

        # Use token
        at = await provider.load_access_token(token.access_token)
        assert at is not None
        assert at.client_id == "my-client"

    @pytest.mark.asyncio
    async def test_preconfigured_client_has_issued_at(self):
        """Pre-configured client has client_id_issued_at set."""
        before = int(time.time())
        provider = BridgeOAuthProvider(
            issuer_url=_ISSUER,
            client_id="my-client",
            client_secret="my-secret",
        )
        after = int(time.time())

        client = await provider.get_client("my-client")
        assert before <= client.client_id_issued_at <= after
