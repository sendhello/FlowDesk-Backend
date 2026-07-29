"""Reserved Sprint-3 endpoints, and proof that the incident stubs are gone.

The last assertion is the important one: when the 501 stubs and the real routes shared a
module, a leftover stub registered before the real handler would have silently shadowed
it. This test fails loudly if that ever happens again.
"""

from __future__ import annotations

import pytest

from app.models.enums import Role
from tests.conftest import login_as, seed_tenant, seed_user


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/notifications",
        "/api/v1/analytics/volume",
        "/api/v1/analytics/status-distribution",
    ],
)
async def test_sprint3_paths_still_return_501(client, db, path):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)

    login_as(admin)
    response = await client.get(path)

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_implemented"


async def test_incident_paths_are_no_longer_reserved(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)

    login_as(admin)
    response = await client.get("/api/v1/incidents")

    assert response.status_code == 200
    assert "data" in response.json()
