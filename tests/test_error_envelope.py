"""The catch-all error envelope (D-1).

Before `ErrorEnvelopeMiddleware` existed, only three typed handlers were registered, so
any unexpected failure fell through to Starlette's default: `500 Internal Server Error` as
plain text, outside the envelope, with no CORS headers. In a browser that is not even a
500 — it is an opaque CORS failure, which is why D-13 read as "the backend is down" for a
sprint.

The CORS assertion below is therefore not cosmetic: it is the difference between the
frontend being able to show the error and the frontend seeing nothing at all. It is also
the reason the fix is a middleware inside CORSMiddleware rather than a handler registered
under `Exception`, which Starlette would place outside it.

`POST /organizations` is the vehicle: it is public, so no auth fixture is needed, and
monkeypatching the service means the request fails before it touches the database.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from app.services import tenant_service

_PAYLOAD = {
    "organization_name": "Acme",
    "admin_email": "admin@acme.com",
    "admin_name": "Admin",
}

#: Stands in for whatever an unhandled exception might carry — a connection string, a
#: service-role key, a row of customer data.
_SECRET = "sk_live_do_not_render_this"


@pytest.fixture
def boom(monkeypatch):
    """Make the organisation service raise something nothing is prepared for."""

    async def _boom(*args, **kwargs):
        raise RuntimeError(_SECRET)

    monkeypatch.setattr(tenant_service, "register_organization", _boom)


def _error(resp) -> dict:
    body = resp.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "details"}, error
    return error


async def test_unhandled_exception_returns_enveloped_500(anon_client, boom):
    resp = await anon_client.post("/api/v1/organizations", json=_PAYLOAD)

    assert resp.status_code == 500, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    error = _error(resp)
    assert error["code"] == "internal_error"
    assert error["details"]["reason"] == "unhandled_exception"


async def test_unhandled_500_carries_an_error_id(anon_client, boom):
    """The client is told exactly one thing about the failure, and it is greppable."""
    resp = await anon_client.post("/api/v1/organizations", json=_PAYLOAD)

    error_id = _error(resp)["details"]["error_id"]
    assert uuid.UUID(hex=error_id)


async def test_unhandled_500_never_renders_the_exception(anon_client, boom):
    """The AppError docstring's rule, applied to the one exception nobody vetted.

    The 502 path has enforced this since Sprint 4
    (test_supabase_failures.test_upstream_body_is_not_echoed_to_the_client); an unhandled
    error is the case where it matters most, because its contents are unknown by
    definition and this endpoint is unauthenticated.
    """
    resp = await anon_client.post("/api/v1/organizations", json=_PAYLOAD)

    assert _SECRET not in resp.text
    assert "RuntimeError" not in resp.text
    assert "Traceback" not in resp.text


async def test_unhandled_500_carries_the_cors_header(anon_client, boom):
    """The test that would have caught the frontend symptom of D-13.

    Fails if ErrorEnvelopeMiddleware is ever moved outside CORSMiddleware, or replaced by
    a handler registered under `Exception` (which Starlette installs as
    ServerErrorMiddleware's handler, outside the whole user middleware stack).
    """
    resp = await anon_client.post(
        "/api/v1/organizations",
        json=_PAYLOAD,
        headers={"Origin": "http://localhost:5173"},
    )

    assert resp.status_code == 500, resp.text
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"


async def test_the_error_id_is_logged_with_a_traceback(anon_client, boom, caplog):
    """The half of the contract the client cannot see: same id, plus the real cause."""
    with caplog.at_level(logging.ERROR, logger="flowdesk.errors"):
        resp = await anon_client.post("/api/v1/organizations", json=_PAYLOAD)

    error_id = _error(resp)["details"]["error_id"]
    assert error_id in caplog.text
    assert "RuntimeError" in caplog.text
    record = next(r for r in caplog.records if error_id in r.getMessage())
    assert record.exc_info is not None
    assert record.name == "flowdesk.errors"


async def test_handled_errors_still_bypass_the_middleware(anon_client):
    """A typed AppError must keep its own status and code, not become a 500.

    A catch-all that swallows too much is worse than none: it would turn every documented
    error into `internal_error` and the frontend would lose every reason slug it branches
    on.
    """
    resp = await anon_client.post("/api/v1/organizations", json={"nope": True})

    assert resp.status_code == 422, resp.text
    assert _error(resp)["code"] == "validation_error"


async def test_health_is_unaffected(anon_client):
    """The middleware sits on the hot path of every request; prove it is transparent."""
    resp = await anon_client.get("/health")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}
