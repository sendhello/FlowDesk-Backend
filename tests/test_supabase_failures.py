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

import uuid

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


# ---- Orphaned auth users after a timed-out invite (D-4) ----------------------------


_INVITE = {"email": "new@acme.com", "name": "New Hire", "role": "staff"}


def _timeout() -> SupabaseUnavailableError:
    """What `_unreachable` raises when the connection drops mid-invite."""
    return SupabaseUnavailableError(
        details={"reason": "identity_provider_unavailable", "operation": "invite_user"},
    )


async def test_invite_timeout_adopts_an_orphaned_auth_user(client, db, fake_supabase):
    """A timeout is the one failure whose outcome is unknown.

    GoTrue may have created the account and sent the invite before the connection dropped.
    Nothing rolled that back, and every retry then hit `already registered` — so the
    address was trapped for good and needed the Supabase dashboard to free. Adopting the
    orphan makes the retry work instead.
    """
    orphan_id = uuid.uuid4()
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)
    fake_supabase.seed_auth_user("new@acme.com", orphan_id)
    fake_supabase.invite_error = _timeout()

    resp = await client.post("/api/v1/users", json=_INVITE)

    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == str(orphan_id)


async def test_invite_timeout_never_adopts_a_claimed_auth_user(client, db, fake_supabase):
    """The safety argument. An auth id that already backs a FlowDesk user IS that user;
    re-pointing it at a new row would hand one person's identity to another."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    claimed = await seed_user(db, tenant, Role.staff, email="someone@acme.com")
    login_as(admin)
    fake_supabase.seed_auth_user("new@acme.com", claimed.id)
    fake_supabase.invite_error = _timeout()

    resp = await client.post("/api/v1/users", json=_INVITE)

    assert resp.status_code == 503, resp.text
    error = _envelope(resp)
    assert error["code"] == "upstream_unavailable"
    # The ORIGINAL error, not a new one invented by the recovery path.
    assert error["details"]["reason"] == "identity_provider_unavailable"


async def test_invite_timeout_without_an_orphan_still_returns_503(
    client, db, fake_supabase
):
    """No account was created, so there is nothing to adopt and nothing to hide."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)
    fake_supabase.invite_error = _timeout()

    resp = await client.post("/api/v1/users", json=_INVITE)

    assert resp.status_code == 503, resp.text
    assert _envelope(resp)["code"] == "upstream_unavailable"


async def test_a_failed_lookup_reports_the_original_error(client, db, fake_supabase):
    """If GoTrue is down for the probe too, the caller hears about the invite, not the
    probe — the recovery attempt must not become the error it reports."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)
    fake_supabase.invite_error = _timeout()
    fake_supabase.lookup_error = SupabaseAdminError(
        "The identity provider rejected the request.",
        details={"reason": "identity_provider_error", "operation": "get_user_by_email"},
    )

    resp = await client.post("/api/v1/users", json=_INVITE)

    assert resp.status_code == 503, resp.text
    assert _envelope(resp)["details"]["operation"] == "invite_user"


async def test_adoption_creates_exactly_one_user_row(client, db, fake_supabase):
    """Adoption provisions; it must not also duplicate."""
    from sqlalchemy import func, select

    from app.models.user import User

    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)
    fake_supabase.seed_auth_user("new@acme.com", uuid.uuid4())
    fake_supabase.invite_error = _timeout()

    assert (await client.post("/api/v1/users", json=_INVITE)).status_code == 201

    total = await db.scalar(
        select(func.count()).select_from(User).where(User.tenant_id == tenant.id)
    )
    assert int(total or 0) == 2  # the admin, plus the adopted invitee


async def test_org_registration_timeout_does_not_adopt(client, db, fake_supabase):
    """Deliberate asymmetry, pinned so nobody "fixes" it later.

    POST /organizations is public, and SupabaseUnavailableError also covers GoTrue's
    invite rate limit — which costs an attacker two requests to trigger. Adopting there
    would let a stranger burn the limit and then bind someone else's address to an
    organisation of their choosing, with the victim's already-sent invite as the way in.
    Recovery on this endpoint stays manual; see the §7.3 question to Fady.
    """
    fake_supabase.seed_auth_user("admin@newco.com", uuid.uuid4())
    fake_supabase.invite_error = _timeout()

    resp = await client.post("/api/v1/organizations", json=_ORG_PAYLOAD)

    assert resp.status_code == 503, resp.text
    assert _envelope(resp)["code"] == "upstream_unavailable"


# ---- The lookup the recovery paths depend on --------------------------------------


async def test_get_user_by_email_walks_every_page(monkeypatch):
    """The lookup used to read one unpaginated page — GoTrue's default 50 rows.

    Past that it answered "no such user" for everyone newer, which silently turned every
    recovery path into a no-op. scripts/seed_demo.py carries a hard 45-user refusal for
    exactly this reason.
    """
    from app.services.supabase_admin import SupabaseAdminClient

    target = uuid.uuid4()
    pages: dict[int, list[dict]] = {
        1: [{"id": str(uuid.uuid4()), "email": f"filler{i}@x.com"} for i in range(200)],
        2: [{"id": str(target), "email": "wanted@acme.com"}],
    }
    seen: list[int] = []

    async def fake_get(self, url, **kwargs):
        page = int(kwargs["params"]["page"])
        seen.append(page)
        return httpx.Response(200, json={"users": pages.get(page, [])})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    found = await SupabaseAdminClient().get_user_by_email("WANTED@acme.com")

    assert found == target
    assert seen == [1, 2]


async def test_get_user_by_email_stops_on_a_short_page(monkeypatch):
    """A page smaller than the request size is the last one — do not keep asking."""
    from app.services.supabase_admin import SupabaseAdminClient

    calls: list[int] = []

    async def fake_get(self, url, **kwargs):
        calls.append(int(kwargs["params"]["page"]))
        return httpx.Response(200, json={"users": [{"id": str(uuid.uuid4()), "email": "a@b.com"}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await SupabaseAdminClient().get_user_by_email("nobody@acme.com") is None
    assert calls == [1]


async def test_get_user_by_email_tolerates_a_null_email(monkeypatch):
    """GoTrue returns the key with a null value for phone-only accounts, and None has no
    .lower() — a latent AttributeError inside a recovery path."""
    from app.services.supabase_admin import SupabaseAdminClient

    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, json={"users": [{"id": str(uuid.uuid4()), "email": None}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await SupabaseAdminClient().get_user_by_email("a@b.com") is None
