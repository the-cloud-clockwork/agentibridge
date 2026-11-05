"""Google sign-in for the OAuth authorization step: the sign-in URL and the ID-token checks."""

from collections.abc import Iterable
from typing import Any, Protocol
from urllib.parse import urlencode

import jwt

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
CALLBACK_PATH = "/oauth/google/callback"
START_PATH = "/oauth/google/start"


class SignInRefused(Exception):
    """Google sign-in did not prove an allowed account."""


class SigningKeys(Protocol):
    """Finds the key that signed a JWT; ``jwt.PyJWKClient`` is one."""

    def get_signing_key_from_jwt(self, token: str) -> Any:
        """The signing key object for ``token``, with the public key in ``.key``."""


def sign_in_url(client_id: str, redirect_uri: str, state: str, nonce: str) -> str:
    """The Google URL that asks for an ID token posted back to ``redirect_uri``."""
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "id_token",
        "response_mode": "form_post",
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode(query)}"


def verified_email(token: str, client_id: str, nonce: str, allowed: Iterable[str], keys: SigningKeys) -> str:
    """The allowed, verified email a Google ID token proves; ``SignInRefused`` for anything else."""
    try:
        key = keys.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=ISSUERS,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise SignInRefused(f"invalid ID token: {exc}") from exc
    if not nonce or claims.get("nonce") != nonce:
        raise SignInRefused("ID token nonce does not match the sign-in request")
    email = str(claims.get("email", "")).lower()
    if claims.get("email_verified") is not True or email not in {entry.lower() for entry in allowed}:
        raise SignInRefused("this Google account is not allowed")
    return email
