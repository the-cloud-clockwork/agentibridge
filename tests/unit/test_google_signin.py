"""Unit tests for the Google sign-in gate on OAuth authorization."""

import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agentibridge.google_signin import CALLBACK_PATH, SignInRefused, sign_in_url, verified_email
from agentibridge.oauth_provider import BridgeOAuthProvider
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

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


async def _started(provider: BridgeOAuthProvider) -> dict[str, str]:
    url = await provider.authorize(_client(), _params())
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


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
    async def test_authorize_sends_the_browser_to_google(self) -> None:
        provider = _provider()

        query = await _started(provider)

        assert query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == f"{ISSUER}{CALLBACK_PATH}"
        assert provider._pending[query["state"]].nonce == query["nonce"]
        assert provider._auth_codes == {}

    @pytest.mark.asyncio
    async def test_allowed_sign_in_issues_the_code_to_the_client(self) -> None:
        provider = _provider()
        query = await _started(provider)

        location = urlparse(provider.finish_google_sign_in(query["state"], _token(nonce=query["nonce"])))
        returned = {key: values[0] for key, values in parse_qs(location.query).items()}

        assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
        assert returned["state"] == "client-state"
        assert provider._auth_codes[returned["code"]].client_id == "mcp-client"

    @pytest.mark.asyncio
    async def test_a_sign_in_request_is_single_use(self) -> None:
        provider = _provider()
        query = await _started(provider)
        signed = _token(nonce=query["nonce"])
        provider.finish_google_sign_in(query["state"], signed)

        with pytest.raises(SignInRefused):
            provider.finish_google_sign_in(query["state"], signed)

    @pytest.mark.asyncio
    async def test_an_expired_sign_in_request_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider()
        query = await _started(provider)
        later = time.time() + 601
        monkeypatch.setattr("agentibridge.oauth_provider.time.time", lambda: later)

        with pytest.raises(SignInRefused):
            provider.finish_google_sign_in(query["state"], _token(nonce=query["nonce"]))

    @pytest.mark.asyncio
    async def test_without_google_the_request_is_approved_directly(self) -> None:
        provider = BridgeOAuthProvider(issuer_url=ISSUER)

        location = await provider.authorize(_client(), _params())

        assert location.startswith(REDIRECT)
        assert len(provider._auth_codes) == 1


@pytest.mark.unit
class TestCallbackRoute:
    @pytest.mark.asyncio
    async def test_valid_post_redirects_and_a_bad_one_is_forbidden(self) -> None:
        provider = _provider()
        good, bad = await _started(provider), await _started(provider)
        app = Starlette(routes=[Route(CALLBACK_PATH, provider.google_callback, methods=["POST"])])
        client = TestClient(app, follow_redirects=False)

        accepted = client.post(CALLBACK_PATH, data={"state": good["state"], "id_token": _token(nonce=good["nonce"])})
        refused = client.post(CALLBACK_PATH, data={"state": bad["state"], "id_token": _token(nonce="wrong")})

        assert accepted.status_code == 302
        assert accepted.headers["location"].startswith(REDIRECT)
        assert refused.status_code == 403
