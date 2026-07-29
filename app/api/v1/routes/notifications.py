"""Notification panel routes (UC-09; US-14).

There is deliberately no `require_role` here: every role receives notifications, so the
only authorisation is "am I the recipient", enforced by
notification_service._recipient_conds. Anything outside it is 404, never 403 — and with no
role gate in front, 404 is the only answer a probe can get.

There is also no collection-level POST. Notifications are system-generated (the write path
is workflow_service -> notification_service, inside the transition's transaction); a client
able to mint its own could forge one against any incident. The path answers 405.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, PageParams, get_current_user, pagination_params
from app.db.session import get_db
from app.schemas.common import Page, Pagination
from app.schemas.notification import MarkAllReadOut, NotificationOut, UnreadCountOut
from app.services import notification_service

router = APIRouter(tags=["notifications"])


@router.get("/notifications", response_model=Page[NotificationOut])
async def list_notifications(
    page: PageParams = Depends(pagination_params),
    is_read: bool | None = Query(default=None),
    actor: CurrentUser = Depends(get_current_user),
    db=Depends(get_db),
) -> Page[NotificationOut]:
    """UC-09 step 5: the caller's notifications, newest first.

    `?is_read=false` is the panel's unread tab. Sort and order are not exposed: UC-09
    specifies exactly one ordering, and adding parameters later is additive.
    """
    rows, total = await notification_service.list_notifications(
        db, actor=actor, is_read=is_read, limit=page.limit, offset=page.offset
    )
    return Page[NotificationOut](
        data=[NotificationOut.model_validate(r) for r in rows],
        pagination=Pagination(limit=page.limit, offset=page.offset, total=total),
    )


# MUST be declared before any /notifications/{notification_id} route: FastAPI resolves in
# registration order, so a dynamic path registered first would parse "unread-count" as a
# uuid and answer 422. There is no such route today; the ordering discipline is kept
# because the next person to add one will not think of this.
@router.get("/notifications/unread-count", response_model=UnreadCountOut)
async def get_unread_count(
    actor: CurrentUser = Depends(get_current_user),
    db=Depends(get_db),
) -> UnreadCountOut:
    """UC-09 step 3: the bell badge."""
    return UnreadCountOut(unread=await notification_service.unread_count(db, actor=actor))


@router.post("/notifications/read-all", response_model=MarkAllReadOut)
async def mark_all_read(
    actor: CurrentUser = Depends(get_current_user),
    db=Depends(get_db),
) -> MarkAllReadOut:
    """UC-09 A1: dismiss all. The response carries the new badge value, so A1.2 needs no
    second round-trip."""
    marked = await notification_service.mark_all_read(db, actor=actor)
    return MarkAllReadOut(marked_read=marked, unread=0)


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID,
    actor: CurrentUser = Depends(get_current_user),
    db=Depends(get_db),
) -> NotificationOut:
    """UC-09 steps 6-7. Marking read is all the backend does; the frontend navigates using
    `incident_id`. The API does not redirect."""
    notification = await notification_service.mark_read(
        db, actor=actor, notification_id=notification_id
    )
    return NotificationOut.model_validate(notification)
