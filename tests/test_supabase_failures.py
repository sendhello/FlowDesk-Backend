"""What the API answers when Supabase Auth itself fails.

Supabase is a dependency, not a part of this service. When GoTrue rate-limits invite
emails (`429 over_email_send_rate_limit`, which the built-in SMTP does after a couple of
messages) or is briefly unreachable, the request cannot be completed — but the cause is
upstream and often transient, and the client is entitled to the same error envelope it
gets everywhere else.

Before this suite existed, `SupabaseAdminError` inherited from `Exception`, so it missed
the `AppError` handler entirely: FastAPI turned it into a bare
`500 {"detail": "Internal Server Error"}` — wrong status, wrong body shape, and no way for
the frontend to tell "try again in a minute" from "this service is broken".
"""

from __future__ import annotations

import httpx
import pytest

from app.models.enums import Role
from app.services.supabase_admin import (
    SupabaseAdminError,
    SupabaseUnavailableError,
)
from tests.conftest import login_as, seed_tenant, seed_user

_ORG_PAYLOAD = {
    "organization_name": "NewCo",
    "admin_email": "admin@newco.com",
    "admin_name": "Admin",
}


def _envelope(resp: httpx.Response) -> dict:
    """Assert the shared error envelope and return `error`."""
    body = resp.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "details"}, error
    return error


# ---- Rate limit (the reported production failure) ---------------------------------


async def test_org_registration_rate_limited_returns_503(client, db, fake_supabase):
    """UC-02 with GoTrue answering 429 over_email_send_rate_limit."""
    fake_supabase.invite_error = SupabaseUnavailableError(
        "The identity provider is temporarily unavailable. Please try again shortly.",
        details={"reason": "identity_provider_unavailable", "operation": "invite_user"},
        retry_after="60",
    )
    resp = await client.post("/api/v1/organizations", json=_ORG_PAYLOAD)

    assert resp.status_code == 503, resp.text
    error = _envelope(resp)
    assert error["code"] == "upstream_unavailable"
    assert error["details"]["reason"] == "identity_provider_unavailable"
    # Retry-After is the whole point of 503 over 500: it tells the client to come back.
    assert resp.headers["retry-after"] == "60"


async def test_user_invite_rate_limited_returns_503(client, db, fake_supabase):
    """UC-03 hits the same GoTrue limit from the authenticated side."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin, email="admin@acme.com")
    login_as(admin)
    fake_supabase.invite_error = SupabaseUnavailableError(
        "The identity provider is temporarily unavailable. Please try again shortly.",
        details={"reason": "identity_provider_unavailable", "operation": "invite_user"},
    )

    resp = await client.post(
        "/api/v1/users",
        json={"email": "new@acme.com", "name": "New", "role": "staff"},
    )

    assert resp.status_code == 503, resp.text
    assert _envelope(resp)["code"] == "upstream_unavailable"
    # No Retry-After upstream means none downstream — never invent a delay.
    assert "retry-after" not in resp.headers


async def test_rate_limited_registration_creates_nothing(client, db, fake_supabase):
    """A refused invite must leave no tenant behind: the DB step never runs."""
    from sqlalchemy import func, select

    from app.models.tenant import Tenant

    fake_supabase.invite_error = SupabaseUnavailableError("upstream down")
    await client.post("/api/v1/organizations", json=_ORG_PAYLOAD)

    total = await db.scalar(select(func.count()).select_from(Tenant))
    assert int(total or 0) == 0


# ---- Non-transient upstream failure ------------------------------------------------


async def test_non_transient_upstream_failure_returns_502(client, db, fake_supabase):
    """A malformed/rejected admin call is not something retrying will fix."""
    fake_supabase.invite_error = SupabaseAdminError(
        "The identity provider rejected the request.",
        details={"reason": "identity_provider_error", "operation": "invite_user"},
    )
    resp = await client.post("/api/v1/organizations", json=_ORG_PAYLOAD)

    assert resp.status_code == 502, resp.text
    error = _envelope(resp)
    assert error["code"] == "upstream_error"
    assert error["details"]["reason"] == "identity_provider_error"


async def test_upstream_body_is_not_echoed_to_the_client(client, db, fake_supabase):
    """The raw GoTrue payload belongs in the log, not in the response.

    Rendering `str(exc)` into the envelope would put upstream JSON — including whatever
    GoTrue chooses to say about the project — in front of an unauthenticated caller, since
    POST /organizations is public.
    """
    fake_supabase.invite_error = SupabaseAdminError(
        "The identity provider rejected the request.",
        details={"reason": "identity_provider_error", "operation": "invite_user"},
    )
    resp = await client.post("/api/v1/organizations", json=_ORG_PAYLOAD)

    assert "over_email_send_rate_limit" not in resp.text
    assert "supabase" not in resp.text.lower()


# ---- Compensation must not mask the real error -------------------------------------


async def test_failed_compensation_does_not_mask_the_conflict(
    client, db, fake_supabase
):
    """UC-03 E1 with the clean-up call also failing.

    The saga deletes the auth user when the DB insert loses a race. If that compensating
    call raises, the caller must still see the 409 that actually describes their request —
    not a 502 about an internal clean-up they did not ask for and cannot act on.
    """
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin, email="admin@acme.com")
    existing = await seed_user(db, tenant, Role.staff, email="taken@acme.com")
    login_as(admin)

    # Same auth id as a user that already exists -> the INSERT violates the PK.
    fake_supabase.next_id = existing.id
    fake_supabase.delete_error = SupabaseUnavailableError("cannot reach GoTrue")

    resp = await client.post(
        "/api/v1/users",
        json={"email": "other@acme.com", "name": "Other", "role": "staff"},
    )

    assert resp.status_code == 409, resp.text
    assert _envelope(resp)["code"] == "conflict"


# ---- Client-level classification ---------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (429, SupabaseUnavailableError),
        (500, SupabaseUnavailableError),
        (503, SupabaseUnavailableError),
        (400, SupabaseAdminError),
        (403, SupabaseAdminError),
    ],
)
async def test_admin_client_classifies_upstream_status(status_code, expected, monkeypatch):
    """429 and 5xx are "come back later"; other 4xx are not.

    Asserted on the real client rather than the fake, so the fake cannot drift from the
    thing it stands in for.
    """
    from app.services.supabase_admin import SupabaseAdminClient

    async def fake_post(self, *args, **kwargs):
        return httpx.Response(
            status_code, json={"code": status_code, "msg": "upstream says no"}
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(expected) as caught:
        await SupabaseAdminClient().invite_user(email="a@b.com", name="A")
    # The narrower class must not be reported for a plain 4xx.
    if expected is SupabaseAdminError:
        assert not isinstance(caught.value, SupabaseUnavailableError)


async def test_admin_client_maps_timeout_to_unavailable(monkeypatch):
    """A network timeout is exactly as transient as a 429, and must not be a 500."""
    from app.services.supabase_admin import SupabaseAdminClient

    async def fake_post(self, *args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(SupabaseUnavailableError):
        await SupabaseAdminClient().invite_user(email="a@b.com", name="A")
