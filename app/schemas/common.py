"""Shared response schemas: pagination envelope and error shape (for OpenAPI docs).

Also the `UNSET` sentinel every PATCH body needs. JSON has one null and a partial update
needs two meanings for it — "leave this alone" and "set this to nothing" — so absence needs
a value of its own. Pydantic records which keys the client actually sent in
`model_fields_set`; `patched()` is how that fact reaches a service signature, where it
would otherwise be lost the moment the model is unpacked into keyword arguments (D-10).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Unset:
    """Type of the `UNSET` singleton. Use `UNSET`, never construct this."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNSET"

    def __bool__(self) -> bool:
        """Falsey, so `value or default` reads the way it looks."""
        return False


#: "The client did not send this field", as distinct from "the client sent null".
UNSET = Unset()


def patched(payload: BaseModel, field: str) -> Any:
    """The submitted value for `field`, or `UNSET` when the client omitted it."""
    return getattr(payload, field) if field in payload.model_fields_set else UNSET


class SortOrder(str, Enum):
    """Sort direction for any paginated list endpoint."""

    asc = "asc"
    desc = "desc"


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
