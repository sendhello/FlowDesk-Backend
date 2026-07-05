"""Thin async wrapper over the Supabase Auth (GoTrue) Admin API.

This is the ONLY place the service-role key is used. The backend never handles raw
passwords — user creation goes through the invite flow, so GoTrue emails the user a
set-password link (UC-02 step 6; NFR-09: bcrypt hashing stays entirely at the Supabase
layer).
"""

from __future__ import annotations

import uuid

import httpx

from app.core.config import settings


class SupabaseAdminError(Exception):
    """A Supabase Admin API call failed unexpectedly."""


class SupabaseUserExistsError(SupabaseAdminError):
    """The email is already registered in Supabase Auth (globally unique)."""


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
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                url, json=payload, params=params, headers=self._headers()
            )
        if resp.status_code in (409, 422) and _looks_like_exists(resp.text):
            raise SupabaseUserExistsError(email)
        if resp.status_code >= 400:
            raise SupabaseAdminError(
                f"invite_user failed: {resp.status_code} {resp.text}"
            )
        return uuid.UUID(resp.json()["id"])

    async def get_user_by_email(self, email: str) -> uuid.UUID | None:
        """Best-effort lookup of an auth user id by email (for idempotent recovery)."""
        url = f"{self._base}/auth/v1/admin/users"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise SupabaseAdminError(
                f"get_user_by_email failed: {resp.status_code} {resp.text}"
            )
        for user in resp.json().get("users", []):
            if user.get("email", "").lower() == email.lower():
                return uuid.UUID(user["id"])
        return None

    async def delete_user(self, user_id: uuid.UUID) -> None:
        """Delete an auth user (used to compensate a failed provisioning saga)."""
        url = f"{self._base}/auth/v1/admin/users/{user_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.delete(url, headers=self._headers())
        if resp.status_code >= 400 and resp.status_code != 404:
            raise SupabaseAdminError(
                f"delete_user failed: {resp.status_code} {resp.text}"
            )


def _looks_like_exists(text: str) -> bool:
    lowered = text.lower()
    return "already" in lowered and ("registered" in lowered or "exists" in lowered)


def get_supabase_admin() -> SupabaseAdminClient:
    """FastAPI dependency. Overridden in tests with a fake client."""
    return SupabaseAdminClient()
