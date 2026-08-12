"""Incident and workflow schemas (UC-06, UC-07, UC-08, UC-11; US-08..US-12)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import IncidentStatus, Severity


class IncidentSort(str, Enum):
    """Sortable incident columns (UC-07 step 6).

    A closed set rather than a free string: FastAPI rejects anything else natively with
    the standard 422 envelope, so no `unsortable_field` reason slug is needed and no
    caller-supplied text ever reaches an ORDER BY clause. Sorting by submitter *name* is
    deferred (it needs a join); a 50-row page can be sorted client-side.
    """

    created_at = "created_at"
    updated_at = "updated_at"
    title = "title"
    severity = "severity"
    status = "status"


class IncidentCreate(BaseModel):
    """UC-06 step 2. `category` is required per UC-06 even though the US-08 text omits it."""

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=10000)
    category_id: uuid.UUID
    severity: Severity


class TransitionCreate(BaseModel):
    """UC-08 step 4-5.

    `note` stays optional here on purpose: UC-08 E2 (required when closing) is a domain
    rule, so it is enforced in workflow_service where the error can carry a
    `details.reason` slug. A Pydantic model_validator would raise RequestValidationError,
    whose envelope has a `details.errors` slot but no `details.reason`.
    """

    to_status: IncidentStatus
    note: str | None = Field(default=None, max_length=2000)


class IncidentAssign(BaseModel):
    """UC-08 A1."""

    assigned_to: uuid.UUID


class UserRef(BaseModel):
    """Minimal user projection embedded in incident payloads."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str


class CategoryRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class IncidentOut(BaseModel):
    """List-row shape — the UC-07 step 5 columns."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    tenant_id: uuid.UUID  # D-7, same reason as UserOut.
    title: str
    severity: Severity
    status: IncidentStatus
    category: CategoryRef
    # Wire names follow the SRS; the validation alias reads the ORM *relationship*, so
    # the FK columns (submitted_by / assigned_to) and the loaded objects never collide.
    submitted_by: UserRef = Field(validation_alias="submitter")
    assigned_to: UserRef | None = Field(default=None, validation_alias="assignee")
    created_at: datetime
    updated_at: datetime


class TransitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    from_status: IncidentStatus
    to_status: IncidentStatus
    transitioned_by: UserRef = Field(validation_alias="actor")
    note: str | None
    created_at: datetime


class IncidentDetail(IncidentOut):
    """UC-11: the full record plus the ordered transition timeline.

    `allowed_transitions` is computed per caller (state + role), so the frontend never
    re-implements the state machine and a future edge change ships backend-only
    (UC-08 steps 1-3, UC-11 step 4). Being caller-dependent, it must not be cached
    across users.
    """

    description: str
    transitions: list[TransitionOut]
    allowed_transitions: list[IncidentStatus]
