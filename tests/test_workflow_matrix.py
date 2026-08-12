"""Exhaustive workflow matrices (card 29): every state pair, every role, every action.

`tests/test_workflow.py` already asserts each interesting cell by hand, and all nine
`(from, to)` pairs happen to be covered today. This file exists because that is a property
of the current enum, not of the test suite: add a fourth `IncidentStatus`, or a `reopen`
edge to `_ALLOWED`, and the hand-written file stays green while seven new cells go
unexercised. The parametrisation below is generated from the enums and from the service's
own public `allowed_transitions`, so a new state or edge is covered the moment it is
declared.

The expected outcome for a cell is derived, never listed:

* the target is legal from the current state -> 200 and one new row;
* the target IS the current state and some legal edge leads to it -> 200 replay, no row
  (NFR-06, resolved over the postcondition — see the workflow_service docstring);
* anything else, including `open -> open` -> 409 UC-08 E1.

One genuine gap this found: `system_admin` against `POST /assign` had no test at all.
Every other role/action cell did.
"""

from __future__ import annotations

import itertools

import pytest

from app.models.enums import IncidentStatus, Role
from app.services.workflow_service import allowed_transitions
from tests.conftest import (
    count_transitions,
    login_as,
    seed_category,
    seed_incident,
    seed_tenant,
    seed_user,
)

E1_MESSAGE = "This transition is not permitted from the current state."

#: Every state some legal edge leads to. Exactly the states for which `from == to` is a
#: replay rather than E1. Read through the public helper so this file never depends on the
#: service's private edge set.
REACHABLE = {
    target
    for state in IncidentStatus
    for target in allowed_transitions(state, Role.reviewer)
}

STATE_PAIRS = list(itertools.product(IncidentStatus, IncidentStatus))


def expected_status(frm: IncidentStatus, to: IncidentStatus) -> int:
    if to in allowed_transitions(frm, Role.reviewer):
        return 200
    if to is frm and to in REACHABLE:
        return 200  # idempotent replay
    return 409


async def _incident_in(db, status: IncidentStatus):
    """One tenant with every role and a single incident already in `status`."""
    tenant = await seed_tenant(db, f"Acme {status.value}")
    staff = await seed_user(db, tenant, Role.staff, email=f"s-{status.value}@t.test")
    reviewer = await seed_user(db, tenant, Role.reviewer, email=f"r-{status.value}@t.test")
    admin = await seed_user(db, tenant, Role.tenant_admin, email=f"a-{status.value}@t.test")
    category = await seed_category(db, tenant)
    incident = await seed_incident(
        db,
        tenant,
        category=category,
        submitted_by=staff,
        assigned_to=reviewer,
        status=status,
    )
    return tenant, staff, reviewer, admin, incident


# ---- The state machine, every cell ------------------------------------------------


@pytest.mark.parametrize("frm,to", STATE_PAIRS, ids=lambda s: s.value)
async def test_every_state_pair_answers_as_the_machine_declares(client, db, frm, to):
    _, _, reviewer, _, incident = await _incident_in(db, frm)
    login_as(reviewer)

    response = await client.post(
        f"/api/v1/incidents/{incident.id}/transitions",
        # A note is always supplied so that UC-08 E2 can never be the reason a cell fails
        # — this parametrisation is about the machine, not about the note rule.
        json={"to_status": to.value, "note": "Matrix probe."},
    )

    assert response.status_code == expected_status(frm, to), response.text


@pytest.mark.parametrize("frm,to", STATE_PAIRS, ids=lambda s: s.value)
async def test_every_refused_pair_reports_what_was_allowed(client, db, frm, to):
    """E1 must be actionable: a client should not have to guess the legal next step."""
    if expected_status(frm, to) != 409:
        pytest.skip("legal edge or replay")
    _, _, reviewer, _, incident = await _incident_in(db, frm)
    login_as(reviewer)

    response = await client.post(
        f"/api/v1/incidents/{incident.id}/transitions",
        json={"to_status": to.value, "note": "Matrix probe."},
    )

    body = response.json()["error"]
    assert body["message"] == E1_MESSAGE
    assert body["details"]["reason"] == "invalid_transition"
    assert body["details"]["from_status"] == frm.value
    assert body["details"]["to_status"] == to.value
    assert body["details"]["allowed"] == [
        s.value for s in allowed_transitions(frm, Role.reviewer)
    ]


@pytest.mark.parametrize("frm,to", STATE_PAIRS, ids=lambda s: s.value)
async def test_only_a_real_edge_writes_a_row(client, db, frm, to):
    """The audit trail is the product's memory: replays and refusals must not pad it."""
    _, _, reviewer, _, incident = await _incident_in(db, frm)
    login_as(reviewer)

    await client.post(
        f"/api/v1/incidents/{incident.id}/transitions",
        json={"to_status": to.value, "note": "Matrix probe."},
    )

    writes_a_row = to in allowed_transitions(frm, Role.reviewer)
    assert await count_transitions(db, incident.id) == (1 if writes_a_row else 0)


# ---- The permission matrix, every cell --------------------------------------------

#: Roles permitted to move an incident through the workflow (UC-08, contract §2.9).
TRANSITION_ROLES = {Role.reviewer, Role.tenant_admin}
#: Roles permitted to reassign (UC-08 A1). System Admin's exclusion is D-6, still open.
REASSIGN_ROLES = {Role.tenant_admin}


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
async def test_transition_permission_matrix(client, db, role):
    tenant, staff, reviewer, admin, incident = await _incident_in(db, IncidentStatus.open)
    actor = {
        Role.staff: staff,
        Role.reviewer: reviewer,
        Role.tenant_admin: admin,
        Role.system_admin: await seed_user(
            db, tenant, Role.system_admin, email="sys@t.test"
        ),
    }[role]
    login_as(actor)

    response = await client.post(
        f"/api/v1/incidents/{incident.id}/transitions",
        json={"to_status": IncidentStatus.in_review.value},
    )

    if role in TRANSITION_ROLES:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["details"]["reason"] == "insufficient_role"


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
async def test_reassign_permission_matrix(client, db, role):
    """`system_admin` had no reassign test before this. It answers 403 like staff and
    reviewer, which is the behaviour D-6 questions — so if D-6 is ever resolved this cell
    fails on purpose rather than letting the change go unnoticed."""
    tenant, staff, reviewer, admin, incident = await _incident_in(db, IncidentStatus.open)
    target = await seed_user(db, tenant, Role.reviewer, email="target@t.test")
    actor = {
        Role.staff: staff,
        Role.reviewer: reviewer,
        Role.tenant_admin: admin,
        Role.system_admin: await seed_user(
            db, tenant, Role.system_admin, email="sys@t.test"
        ),
    }[role]
    login_as(actor)

    response = await client.post(
        f"/api/v1/incidents/{incident.id}/assign", json={"assigned_to": str(target.id)}
    )

    if role in REASSIGN_ROLES:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["details"]["reason"] == "insufficient_role"


@pytest.mark.parametrize(
    "status,role",
    list(itertools.product(IncidentStatus, Role)),
    ids=lambda v: v.value,
)
async def test_allowed_transitions_is_consistent_with_what_the_endpoint_accepts(
    client, db, status, role
):
    """`allowed_transitions` exists so the frontend renders the right buttons without
    re-implementing the machine. If it ever advertises a move the endpoint then refuses,
    the UI grows a button that returns an error — so the two are asserted against each
    other rather than each against a hand-written list."""
    advertised = allowed_transitions(status, role)

    if role not in TRANSITION_ROLES:
        assert advertised == []
        return

    for target in advertised:
        _, _, reviewer, admin, incident = await _incident_in(db, status)
        login_as(reviewer if role is Role.reviewer else admin)
        response = await client.post(
            f"/api/v1/incidents/{incident.id}/transitions",
            json={"to_status": target.value, "note": "Advertised move."},
        )
        assert response.status_code == 200, response.text
