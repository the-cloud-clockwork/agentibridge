"""Unit tests for the Google sign-in gate on OAuth authorization."""

import dataclasses
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agentibridge.google_signin import CALLBACK_PATH, START_PATH, SignInRefused, sign_in_url, verified_email
from agentibridge.oauth_provider import BridgeOAuthProvider, binding_cookie

CLIENT_ID = "client-under-test.apps.googleusercontent.com"
ISSUER = "https://bridge.example.com"
REDIRECT = "https://client.example.com/callback"
ALLOWED = "owner@example.com"
SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FixedKeys:
    """Signing keys that always answer with one public key."""

    def __init__(self, signer: rsa.RSAPrivateKey) -> None:
        self.public_key = signer.public_key()

    def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
        return SimpleNamespace(key=self.public_key)


def _token(signer: rsa.RSAPrivateKey = SIGNER, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1234",
        "iat": now,
        "exp": now + 300,
        "nonce": "nonce-1",
        "email": ALLOWED,
        "email_verified": True,
        **overrides,
    }
    return jwt.encode({key: value for key, value in claims.items() if value is not None}, signer, "RS256")


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id="mcp-client", redirect_uris=[AnyUrl(REDIRECT)])


def _params() -> AuthorizationParams:
    return AuthorizationParams(
        state="client-state",
        scopes=["claudeai"],
        code_challenge="challenge",
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


def _provider() -> BridgeOAuthProvider:
    provider = BridgeOAuthProvider(
        issuer_url=ISSUER, google_client_id=CLIENT_ID, allowed_emails=[" Owner@Example.com "]
    )
    provider._signing_keys = FixedKeys(SIGNER)
    return provider


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


async def _parked(provider: BridgeOAuthProvider) -> str:
    return _query(await provider.authorize(_client(), _params()))["state"]


async def _started(provider: BridgeOAuthProvider) -> dict[str, str]:
    state = await _parked(provider)
    google, binding = provider.begin_google_sign_in(state)
    return {**_query(google), "binding": binding}


def _browser(provider: BridgeOAuthProvider) -> TestClient:
    app = Starlette(
        routes=[
            Route(START_PATH, provider.google_start, methods=["GET"]),
            Route(CALLBACK_PATH, provider.google_callback, methods=["POST"]),
        ]
    )
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


@pytest.mark.unit
class TestSignInUrl:
    def test_asks_google_for_an_id_token_posted_back(self) -> None:
        url = urlparse(sign_in_url(CLIENT_ID, f"{ISSUER}{CALLBACK_PATH}", "state-1", "nonce-1"))
        query = {key: values[0] for key, values in parse_qs(url.query).items()}

        assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
        assert query == {
            "client_id": CLIENT_ID,
            "redirect_uri": f"{ISSUER}/oauth/google/callback",
            "response_type": "id_token",
            "response_mode": "form_post",
            "scope": "openid email",
            "state": "state-1",
            "nonce": "nonce-1",
            "prompt": "select_account",
        }


@pytest.mark.unit
class TestVerifiedEmail:
    def test_allowed_verified_account_passes(self) -> None:
        assert verified_email(_token(), CLIENT_ID, "nonce-1", [ALLOWED], FixedKeys(SIGNER)) == ALLOWED

    @pytest.mark.parametrize(
        "token",
        [
            _token(aud="someone-else.apps.googleusercontent.com"),
            _token(iss="https://evil.example.com"),
            _token(exp=int(time.time()) - 60),
            _token(nonce="replayed"),
            _token(email_verified=False),
            _token(email="intruder@example.com"),
            _token(signer=OTHER_SIGNER),
            "not-a-jwt",
        ],
        ids=["audience", "issuer", "expired", "nonce", "unverified", "not-allowed", "signature", "malformed"],
    )
    def test_any_failed_check_is_refused(self, token: str) -> None:
        with pytest.raises(SignInRefused):
            verified_email(token, CLIENT_ID, "nonce-1", [ALLOWED], FixedKeys(SIGNER))

    def test_empty_nonce_is_refused(self) -> None:
        with pytest.raises(SignInRefused):
            verified_email(_token(nonce=None), CLIENT_ID, "", [ALLOWED], FixedKeys(SIGNER))


@pytest.mark.unit
class TestProviderGate:
    @pytest.mark.asyncio
    async def test_authorize_sends_the_browser_to_the_bridge_start_page(self) -> None:
        provider = _provider()

        url = urlparse(await provider.authorize(_client(), _params()))
        state = _query(url.geturl())["state"]

        assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}{START_PATH}"
        assert state in provider._pending
        assert provider._auth_codes == {}

    @pytest.mark.asyncio
    async def test_begin_hands_out_the_google_url_once(self) -> None:
        provider = _provider()
        state = await _parked(provider)

        google, binding = provider.begin_google_sign_in(state)
        query = _query(google)

        assert query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == f"{ISSUER}{CALLBACK_PATH}"
        assert query["state"] == state
        assert query["nonce"] == provider._pending[state].nonce
        assert binding == provider._pending[state].binding
        with pytest.raises(SignInRefused):
            provider.begin_google_sign_in(state)

    @pytest.mark.asyncio
    async def test_allowed_sign_in_issues_the_code_to_the_client(self) -> None:
        provider = _provider()
        query = await _started(provider)

        location = provider.finish_google_sign_in(query["state"], _token(nonce=query["nonce"]), query["binding"])
        returned = _query(location)

        assert location.startswith(REDIRECT)
        assert returned["state"] == "client-state"
        assert provider._auth_codes[returned["code"]].client_id == "mcp-client"

    @pytest.mark.asyncio
    async def test_a_sign_in_finished_in_another_browser_is_refused(self) -> None:
        provider = _provider()
        query = await _started(provider)

        with pytest.raises(SignInRefused, match="not started in this browser"):
            provider.finish_google_sign_in(query["state"], _token(nonce=query["nonce"]), "")
        location = provider.finish_google_sign_in(query["state"], _token(nonce=query["nonce"]), query["binding"])

        assert location.startswith(REDIRECT)

    @pytest.mark.asyncio
    async def test_a_sign_in_never_started_at_the_bridge_is_refused(self) -> None:
        provider = _provider()
        state = await _parked(provider)
        pending = provider._pending[state]

        with pytest.raises(SignInRefused):
            provider.finish_google_sign_in(state, _token(nonce=pending.nonce), pending.binding)

    @pytest.mark.asyncio
    async def test_a_sign_in_request_is_single_use(self) -> None:
        provider = _provider()
        query = await _started(provider)
        signed = _token(nonce=query["nonce"])
        provider.finish_google_sign_in(query["state"], signed, query["binding"])

        with pytest.raises(SignInRefused):
            provider.finish_google_sign_in(query["state"], signed, query["binding"])

    @pytest.mark.asyncio
    async def test_an_expired_sign_in_request_is_refused(self) -> None:
        provider = _provider()
        query = await _started(provider)
        state = query["state"]
        provider._pending[state] = dataclasses.replace(provider._pending[state], expires_at=time.time() - 1)

        with pytest.raises(SignInRefused):
            provider.finish_google_sign_in(state, _token(nonce=query["nonce"]), query["binding"])

    @pytest.mark.asyncio
    async def test_pending_sign_ins_are_capped_and_expired_ones_make_room(self) -> None:
        provider = _provider()
        states = [await _parked(provider) for _ in range(64)]

        with pytest.raises(AuthorizeError) as refused:
            await provider.authorize(_client(), _params())
        provider._pending[states[0]] = dataclasses.replace(provider._pending[states[0]], expires_at=time.time() - 1)
        await provider.authorize(_client(), _params())

        assert refused.value.error == "temporarily_unavailable"
        assert states[0] not in provider._pending
        assert len(provider._pending) == 64

    @pytest.mark.asyncio
    async def test_without_google_the_request_is_approved_directly(self) -> None:
        provider = BridgeOAuthProvider(issuer_url=ISSUER)

        location = await provider.authorize(_client(), _params())

        assert location.startswith(REDIRECT)
        assert len(provider._auth_codes) == 1


@pytest.mark.unit
class TestSignInRoutes:
    @pytest.mark.asyncio
    async def test_start_sets_a_callback_scoped_cookie_and_redirects_to_google(self) -> None:
        provider = _provider()
        state = await _parked(provider)
        browser = _browser(provider)

        started = browser.get(START_PATH, params={"state": state})
        again = browser.get(START_PATH, params={"state": state})
        cookie = started.headers["set-cookie"].lower()

        assert started.status_code == 302
        assert started.headers["location"].startswith("https://accounts.google.com/")
        assert cookie.startswith(f"{binding_cookie(state).lower()}={provider._pending[state].binding.lower()}")
        assert all(part in cookie for part in ("httponly", "secure", "samesite=none", f"path={CALLBACK_PATH}"))
        assert again.status_code == 403

    @pytest.mark.asyncio
    async def test_callback_needs_the_browser_that_started_the_sign_in(self) -> None:
        provider = _provider()
        good, stolen = await _parked(provider), await _parked(provider)
        owner, attacker = _browser(provider), _browser(provider)
        owner.get(START_PATH, params={"state": good})
        attacker.get(START_PATH, params={"state": stolen})

        accepted = owner.post(
            CALLBACK_PATH, data={"state": good, "id_token": _token(nonce=provider._pending[good].nonce)}
        )
        refused = owner.post(
            CALLBACK_PATH, data={"state": stolen, "id_token": _token(nonce=provider._pending[stolen].nonce)}
        )

        assert accepted.status_code == 302
        assert accepted.headers["location"].startswith(REDIRECT)
        assert refused.status_code == 403

    @pytest.mark.asyncio
    async def test_callback_refuses_a_bad_token_and_an_oversized_body(self) -> None:
        provider = _provider()
        state = await _parked(provider)
        browser = _browser(provider)
        browser.get(START_PATH, params={"state": state})

        oversized = browser.post(CALLBACK_PATH, data={"state": state, "id_token": "x" * 20000})
        bad = browser.post(CALLBACK_PATH, data={"state": state, "id_token": _token(nonce="wrong")})

        assert oversized.status_code == 413
        assert bad.status_code == 403
