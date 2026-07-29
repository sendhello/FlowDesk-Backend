"""Application configuration.

All configuration is sourced from environment variables (Appendix B.1: "Environment
variables hold all configuration. No hardcoded credentials are permitted"). In local
development the values are read from a `.env` file; in production they come from Fly
secrets and GitHub Actions secrets.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- App ----
    app_env: str = "development"
    log_level: str = "info"
    cors_origins: str = "http://localhost:5173"
    # Where Supabase redirects the emailed invite/set-password link. Must be listed in
    # the Supabase project's Auth "Redirect URLs", or GoTrue falls back to the Site URL.
    invite_redirect_url: str = "https://flowdesk.vanelsen.net.au/set-password"

    # ---- Database ----
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/flowdesk"

    # ---- Supabase ----
    supabase_url: str = "https://example.supabase.co"
    supabase_project_ref: str = ""
    supabase_service_role_key: str = ""
    supabase_jwks_url: str = (
        "https://example.supabase.co/auth/v1/.well-known/jwks.json"
    )

    # ---- JWT verification (asymmetric) ----
    jwt_audience: str = "authenticated"
    jwt_issuer: str = "https://example.supabase.co/auth/v1"

    # Supabase Admin API base (derived from supabase_url).
    admin_api_timeout_seconds: float = Field(default=10.0)

    # ---- Reporting / analytics (UC-10) ----
    # `date_trunc('week', <timestamptz>)` renders the value in the SESSION TimeZone, which
    # is UTC on Fly. An incident submitted Monday 09:00 Melbourne is Sunday 23:00 UTC and
    # would be bucketed into the previous week, so every analytics query converts
    # explicitly with AT TIME ZONE. FlowDesk is single-timezone for the MVP: there is no
    # `tenants.timezone` column and adding one is out of Sprint-3 scope. Pending
    # confirmation from the product owner.
    reporting_timezone: str = "Australia/Melbourne"

    @field_validator("reporting_timezone")
    @classmethod
    def _validate_reporting_timezone(cls, value: str) -> str:
        """Fail at start-up, not per request.

        Python's zoneinfo and PostgreSQL both read the IANA tzdb, so a name accepted here
        is a name `AT TIME ZONE` accepts. Without this check a typo would surface as a
        PostgreSQL error on every analytics call instead of a refusal to boot.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown IANA timezone: {value!r}") from exc
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins as a list (env var is a comma-separated string)."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def supabase_admin_users_url(self) -> str:
        """Supabase GoTrue admin endpoint for user management."""
        return f"{self.supabase_url.rstrip('/')}/auth/v1/admin/users"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (one read of the environment per process)."""
    return Settings()


settings = get_settings()
