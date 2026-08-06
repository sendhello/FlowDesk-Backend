"""Thin async wrapper over the Supabase Auth (GoTrue) Admin API.

This is the ONLY place the service-role key is used. The backend never handles raw
passwords — user creation goes through the invite flow, so GoTrue emails the user a
set-password link (UC-02 step 6; NFR-09: bcrypt hashing stays entirely at the Supabase
layer).

Failure vocabulary
------------------
`SupabaseAdminError` is an `AppError`, so a failed admin call renders in the shared error
envelope through the handler in app.core.exceptions. It used to inherit from `Exception`,
which meant every upstream refusal — most visibly GoTrue's `429 over_email_send_rate_limit`,
which the built-in SMTP returns after a couple of invites — escaped unhandled and reached
the client as a bare `500 {"detail": "Internal Server Error"}`: wrong status, and outside
the one envelope the whole API otherwise guarantees. A rate limit at the identity provider
is not an internal server error, and the caller can act on it.

Two distinctions, deliberately kept apart:

* **Transient or not.** 429, 5xx and network timeouts raise `SupabaseUnavailableError`
  (503, with `Retry-After` when GoTrue supplies one). Any other 4xx raises a plain
  `SupabaseAdminError` (502) — repeating that request will not help.
* **A signal, not an outcome.** `SupabaseUserExistsError` is neither of the above: it is a
  fact the caller interprets. `tenant_service` reads it as a 409; `user_service` reads it
  as "provision against the account that already exists" (UC-03's recovery path). Its
  status code is a defensive default — reaching the handler means a call site forgot to
  catch it.

The upstream response body is logged, never rendered. `POST /organizations` is public, so
whatever ends up in `exc.message` is readable by an unauthenticated caller.
"""

from __future__ import annotations

import uuid
from typing import NoReturn

import httpx

from app.core.config import settings
from app.core.exceptions import UpstreamServiceError, UpstreamUnavailableError
from app.core.logging import get_logger, log_compensation_failure

logger = get_logger(__name__)

_REJECTED_MESSAGE = "The identity provider rejected the request."


class SupabaseAdminError(UpstreamServiceError):
    """A Supabase Admin API call failed. Renders as 502 in the shared envelope."""


class SupabaseUnavailableError(SupabaseAdminError, UpstreamUnavailableError):
    """...for a transient reason: a rate limit, a 5xx, or a network timeout. 503.

    Inherits from both so `except SupabaseAdminError` still catches it (the call sites
    reason about "the admin API failed") while the status code and error code come from
    the transient branch of the upstream vocabulary.
    """

    status_code = UpstreamUnavailableError.status_code
    code = UpstreamUnavailableError.code

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(
            message
            or "The identity provider is temporarily unavailable. "
            "Please try again shortly.",
            details=details,
            headers={"Retry-After": retry_after} if retry_after else None,
        )


class SupabaseUserExistsError(SupabaseAdminError):
    """The email is already registered in Supabase Auth (globally unique)."""

    status_code = 409
    code = "conflict"


def _fail(operation: str, response: httpx.Response) -> NoReturn:
    """Classify a failed GoTrue response and raise. Logs the body; never renders it."""
    logger.error(
        "supabase_admin %s failed: %s %s", operation, response.status_code, response.text
    )
    if response.status_code == 429 or response.status_code >= 500:
        raise SupabaseUnavailableError(
            details={"reason": "identity_provider_unavailable", "operation": operation},
            retry_after=response.headers.get("retry-after"),
        )
    raise SupabaseAdminError(
        _REJECTED_MESSAGE,
        details={"reason": "identity_provider_error", "operation": operation},
    )


def _unreachable(operation: str, exc: httpx.HTTPError) -> NoReturn:
    """A timeout or connection failure is exactly as transient as a 429."""
    logger.error("supabase_admin %s unreachable: %r", operation, exc)
    raise SupabaseUnavailableError(
        details={"reason": "identity_provider_unavailable", "operation": operation},
    ) from exc


class SupabaseAdminClient:
    """Async client for the GoTrue admin endpoints."""

    def __init__(self) -> None:
        self._base = settings.supabase_url.rstrip("/")
        self._key = settings.supabase_service_role_key
        self._timeout = settings.admin_api_timeout_seconds
        self._invite_redirect = settings.invite_redirect_url

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    async def invite_user(
        self, *, email: str, name: str, redirect_to: str | None = None
    ) -> uuid.UUID:
        """Invite a user (sends a set-password email). Returns the new auth user id.

        GoTrue redirects the emailed link to ``redirect_to`` (defaults to
        settings.invite_redirect_url — the frontend /set-password page). The URL must be
        whitelisted in the Supabase project's Auth "Redirect URLs", otherwise GoTrue
        silently falls back to the project Site URL.
        """
        url = f"{self._base}/auth/v1/invite"
        payload = {"email": email, "data": {"name": name}}
        target = redirect_to if redirect_to is not None else self._invite_redirect
        params = {"redirect_to": target} if target else None
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url, json=payload, params=params, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            _unreachable("invite_user", exc)
        if resp.status_code in (409, 422) and _looks_like_exists(resp.text):
            raise SupabaseUserExistsError(email)
        if resp.status_code >= 400:
            _fail("invite_user", resp)
        return uuid.UUID(resp.json()["id"])

    async def get_user_by_email(self, email: str) -> uuid.UUID | None:
        """Best-effort lookup of an auth user id by email (for idempotent recovery)."""
        url = f"{self._base}/auth/v1/admin/users"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            _unreachable("get_user_by_email", exc)
        if resp.status_code >= 400:
            _fail("get_user_by_email", resp)
        for user in resp.json().get("users", []):
            if user.get("email", "").lower() == email.lower():
                return uuid.UUID(user["id"])
        return None

    async def delete_user(self, user_id: uuid.UUID) -> None:
        """Delete an auth user (used to compensate a failed provisioning saga)."""
        url = f"{self._base}/auth/v1/admin/users/{user_id}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.delete(url, headers=self._headers())
        except httpx.HTTPError as exc:
            _unreachable("delete_user", exc)
        if resp.status_code >= 400 and resp.status_code != 404:
            _fail("delete_user", resp)


async def delete_user_best_effort(
    admin: SupabaseAdminClient, user_id: uuid.UUID, *, action: str
) -> None:
    """Compensating delete for a failed provisioning saga. Records failure, never raises.

    Both sagas (Part 3A.3) delete the auth user when their DB half fails, then raise the
    error that describes the caller's request. If the delete itself raised, it would
    replace that error: the caller would get a 502 about our internal clean-up instead of
    the 409 about their own duplicate email, and would have no way to act on it. There is
    also nothing left to try — so the orphan is logged for a human and the original error
    is allowed through.
    """
    try:
        await admin.delete_user(user_id)
    except SupabaseAdminError as exc:
        log_compensation_failure(
            action=action, auth_user_id=str(user_id), error=repr(exc)
        )


def _looks_like_exists(text: str) -> bool:
    lowered = text.lower()
    return "already" in lowered and ("registered" in lowered or "exists" in lowered)


def get_supabase_admin() -> SupabaseAdminClient:
    """FastAPI dependency. Overridden in tests with a fake client."""
    return SupabaseAdminClient()
