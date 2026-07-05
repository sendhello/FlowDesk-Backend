"""Shared response schemas: pagination envelope and error shape (for OpenAPI docs)."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Pagination(BaseModel):
    limit: int
    offset: int
    total: int


class Page(BaseModel, Generic[T]):
    """Standard list response: `{"data": [...], "pagination": {...}}`."""

    data: list[T]
    pagination: Pagination


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict = {}


class ErrorEnvelope(BaseModel):
    error: ErrorBody
