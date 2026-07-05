"""Pytest fixtures.

DB-backed tests run against a real PostgreSQL (native enums, UUIDs and constraints must
behave like production). The connection string comes from `TEST_DATABASE_URL`; if it is
unset, the DB fixtures skip so unit-only tests (e.g. JWT) still run.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import deps
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.enums import Role, UserStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.services.supabase_admin import (
    SupabaseUserExistsError,
    get_supabase_admin,
)

TEST_DB_URL = os.getenv("TEST_DATABASE_URL")

_TABLES = [
    "notifications",
    "workflow_transitions",
    "incidents",
    "categories",
    "users",
    "tenants",
]


@pytest_asyncio.fixture
async def engine():
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set — DB-backed test skipped")
    eng = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(sessionmaker):
    async with sessionmaker() as session:
        yield session


@pytest_asyncio.fixture
async def client(sessionmaker):
    async def _get_db():
        async with sessionmaker() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    fastapi_app.dependency_overrides.clear()


# ---- Auth override helpers ------------------------------------------------------


def as_current_user(user: User) -> deps.CurrentUser:
    return deps.CurrentUser(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        name=user.name,
        role=user.role,
        status=user.status,
    )


def login_as(user: User) -> None:
    """Inject `user` as the authenticated caller for the app under test."""
    fastapi_app.dependency_overrides[deps.get_current_user] = lambda: as_current_user(user)


def login_claims(sub: str) -> None:
    """Inject raw JWT claims so the real get_current_user + DB logic is exercised."""
    fastapi_app.dependency_overrides[deps.get_current_claims] = lambda: {"sub": sub}


# ---- Seed helpers ---------------------------------------------------------------


async def seed_tenant(db, name: str) -> Tenant:
    tenant = Tenant(name=name)
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


async def seed_user(
    db,
    tenant: Tenant,
    role: Role,
    *,
    email: str | None = None,
    status: UserStatus = UserStatus.active,
) -> User:
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email=email or f"{uuid.uuid4().hex[:8]}@example.com",
        name="Test User",
        role=role,
        status=status,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ---- Fake Supabase Admin client -------------------------------------------------


class FakeSupabaseAdmin:
    """In-memory stand-in for SupabaseAdminClient (no network)."""

    def __init__(self) -> None:
        self.next_id: uuid.UUID | None = None
        self.existing_emails: set[str] = set()
        self.invited: list[uuid.UUID] = []
        self.deleted: list[uuid.UUID] = []
        self._by_email: dict[str, uuid.UUID] = {}

    async def invite_user(self, *, email: str, name: str) -> uuid.UUID:
        if email.lower() in self.existing_emails:
            raise SupabaseUserExistsError(email)
        uid = self.next_id or uuid.uuid4()
        self._by_email[email.lower()] = uid
        self.invited.append(uid)
        return uid

    async def get_user_by_email(self, email: str) -> uuid.UUID | None:
        return self._by_email.get(email.lower())

    async def delete_user(self, user_id: uuid.UUID) -> None:
        self.deleted.append(user_id)


@pytest.fixture
def fake_supabase():
    fake = FakeSupabaseAdmin()
    fastapi_app.dependency_overrides[get_supabase_admin] = lambda: fake
    yield fake
    fastapi_app.dependency_overrides.pop(get_supabase_admin, None)
