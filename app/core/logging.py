"""Structured logging and an audit trail.

NFR-07 requires the system to log all authentication failures and workflow transitions
for audit purposes. Privileged mutations (org creation, user invite, role change,
deactivation) are also audited here; workflow-transition audit lands in Sprint 2.
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


def _fmt(context: dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in context.items())
