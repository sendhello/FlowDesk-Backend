"""JWT verification unit tests (US-07, NFR-11). No DB required.

A local RSA keypair stands in for Supabase's JWKS: the JWKS client is monkeypatched to
return our public key, and tokens are signed with the matching private key.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core import security
from app.core.config import settings
from app.core.exceptions import UnauthorizedError

SUB = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def patch_jwks(monkeypatch, rsa_key):
    class _Key:
        def __init__(self, key):
            self.key = key

    class _Client:
        def get_signing_key_from_jwt(self, _token):
            return _Key(rsa_key.public_key())

    monkeypatch.setattr(security, "_get_jwks_client", lambda: _Client())


def _make_token(rsa_key, **overrides) -> str:
    now = int(time.time())
    payload = {
        "sub": overrides.get("sub", SUB),
        "aud": overrides.get("aud", settings.jwt_audience),
        "iss": overrides.get("iss", settings.jwt_issuer),
        "exp": overrides.get("exp", now + 3600),
        "iat": now,
    }
    signing_key = overrides.get("key", rsa_key)
    return jwt.encode(payload, signing_key, algorithm="RS256")


def test_valid_token_returns_claims(rsa_key):
    claims = security.verify_jwt(_make_token(rsa_key))
    assert claims["sub"] == SUB


def test_expired_token_raises_401(rsa_key):
    with pytest.raises(UnauthorizedError) as exc:
        security.verify_jwt(_make_token(rsa_key, exp=int(time.time()) - 10))
    assert exc.value.details["reason"] == "token_expired"


def test_wrong_audience_raises_401(rsa_key):
    with pytest.raises(UnauthorizedError):
        security.verify_jwt(_make_token(rsa_key, aud="some-other-audience"))


def test_wrong_issuer_raises_401(rsa_key):
    with pytest.raises(UnauthorizedError):
        security.verify_jwt(_make_token(rsa_key, iss="https://evil.example.com/auth/v1"))


def test_tampered_signature_raises_401(rsa_key):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(UnauthorizedError):
        security.verify_jwt(_make_token(rsa_key, key=other_key))
