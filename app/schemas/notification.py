"""Notification schemas (UC-09; US-13, US-14)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationOut(BaseModel):
    """One row of the notification panel (UC-09 step 5).

    No `user_id` and no `tenant_id`: the caller IS the user, so echoing them back is noise,
    and NFR-05 says not to surface internal identifiers without a purpose. `incident_id`
    is present because UC-09 step 7 navigates to the incident detail page from here.

    The incident's title and status are not embedded either — the title is already inside
    `message`, and a stale status in a notification would contradict the incident page.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    incident_id: uuid.UUID
    message: str
    is_read: bool
    created_at: datetime


class UnreadCountOut(BaseModel):
    """UC-09 step 3: the bell badge.

    A dedicated endpoint rather than a field on the list envelope. The badge renders on
    every page while the panel opens rarely, so it should not cost a paginated query; and
    `Page[T]` is a shared generic — one endpoint's convenience must not deform it.
    """

    unread: int


class MarkAllReadOut(UnreadCountOut):
    """UC-09 A1. `unread` is always 0 (A1.2: "the unread count resets to zero"), so the
    frontend can reuse one badge-setter for this response and for the unread-count
    endpoint. `marked_read` is how many rows this call actually flipped."""

    marked_read: int
