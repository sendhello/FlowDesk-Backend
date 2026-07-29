"""Structured logging and an audit trail.

NFR-07 requires the system to log all authentication failures and workflow transitions
for audit purposes. Privileged mutations (org creation, user invite, role change,
deactivation, incident reassignment) are audited by `log_privileged_action`; workflow
state changes by `log_workflow_transition`.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from app.core.config import settings

_AUDIT_LOGGER_NAME = "flowdesk.audit"


def configure_logging() -> None:
    """Configure root logging once, at application start-up."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


_audit = logging.getLogger(_AUDIT_LOGGER_NAME)


def log_auth_failure(reason: str, **context: Any) -> None:
    """NFR-07: record an authentication failure."""
    _audit.warning("auth_failure reason=%s %s", reason, _fmt(context))


def log_privileged_action(action: str, *, actor_id: str, **context: Any) -> None:
    """NFR-07: record a privileged mutation performed by an admin."""
    _audit.info("privileged_action action=%s actor=%s %s", action, actor_id, _fmt(context))


def log_workflow_transition(
    *,
    incident_id: str,
    actor_id: str,
    from_status: str,
    to_status: str,
    **context: Any,
) -> None:
    """NFR-07: record an incident workflow state change (UC-08 step 8).

    Called after the commit, so an audit line only ever describes a durable fact.
    Idempotent replays are not logged: nothing changed, so there is nothing to audit.
    """
    _audit.info(
        "workflow_transition incident=%s actor=%s from=%s to=%s %s",
        incident_id,
        actor_id,
        from_status,
        to_status,
        _fmt(context),
    )


def _fmt(context: dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in context.items())
