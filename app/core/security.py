"""JWT verification against Supabase's JWKS endpoint.

Best practice (2025+): verify Supabase access tokens asymmetrically using the project's
published public keys (RS256/ES256) fetched from the JWKS endpoint, NOT the legacy shared
HS256 secret. `PyJWKClient` fetches and caches the signing keys by `kid`, so verification
is fast and offline after the first fetch, and key rotation is zero-downtime.

Satisfies: US-07 (verify JWT on every request), NFR-11 (reject expired tokens -> 401).
"""

from __future__ import annotations

from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.core.config import settings
from app.core.exceptions import UnauthorizedError

# Asymmetric algorithms only. HS256 (shared secret) is intentionally excluded.
_ALGORITHMS = ["RS256", "ES256"]


@lru_cache
def _get_jwks_client() -> PyJWKClient:
    """Lazily build a cached JWKS client. Overridden in tests with a local key."""
    return PyJWKClient(settings.supabase_jwks_url)


def verify_jwt(token: str) -> dict:
    """Verify a Supabase JWT's signature, issuer, audience and expiry.

    Returns the decoded claims on success. Raises UnauthorizedError (-> HTTP 401) on any
    failure, including an expired token (NFR-11).
    """
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=_ALGORITHMS,
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:  # NFR-11
        raise UnauthorizedError(
            "Token has expired.", details={"reason": "token_expired"}
        ) from exc
    except jwt.PyJWTError as exc:
        raise UnauthorizedError(
            "Invalid authentication token.", details={"reason": "invalid_token"}
        ) from exc
    except Exception as exc:  # JWKS fetch/key resolution failure
        raise UnauthorizedError(
            "Unable to verify authentication token.",
            details={"reason": "verification_failed"},
        ) from exc
