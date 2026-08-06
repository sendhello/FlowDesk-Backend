"""Application error hierarchy and FastAPI exception handlers.

Every non-2xx response uses a single, consistent error envelope so the frontend can
rely on one shape (part of the Ivan <-> Brad API contract):

    {"error": {"code": "<machine_slug>", "message": "<human text>", "details": {...}}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """Base class for all handled application errors.

    `message` and `details` are RENDERED TO THE CLIENT. Never build either from an upstream
    response body or an internal exception string — put that in the log instead.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.__class__.__name__
        self.details = details or {}
        #: Response headers this error carries, e.g. `Retry-After` on a 503.
        self.headers = headers or {}
        super().__init__(self.message)


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ValidationError(AppError):
    status_code = 422  # Unprocessable Content
    code = "validation_error"


class UpstreamServiceError(AppError):
    """A dependency this service does not control refused or could not answer.

    502, not 500: the request failed, but nothing in FlowDesk is broken, and the
    distinction matters to whoever is on call. Supabase Auth is the only such dependency
    today (see app/services/supabase_admin.py) — every other failure mode is ours.

    Retrying will not help; that is what the subclass below is for.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_error"


class UpstreamUnavailableError(UpstreamServiceError):
    """...and the reason is transient: a rate limit, a 5xx or a timeout.

    503 plus `Retry-After` where the upstream supplied one, so the same request is worth
    repeating. GoTrue's built-in SMTP rate limit (`429 over_email_send_rate_limit`) is the
    case this exists for: it is hit routinely on the free tier and is not an error in the
    caller's request at all.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "upstream_unavailable"


class NotImplementedYetError(AppError):
    """Vocabulary for a reserved endpoint: a stable contract that is honest about not
    being built yet.

    Currently unreferenced — Sprint 3 implemented the last reserved paths and deleted
    `routes/reserved.py`. Kept because `not_implemented` is a documented `error.code` in
    the frontend contract, so removing the class would be a contract deletion for no gain.
    """

    status_code = status.HTTP_501_NOT_IMPLEMENTED
    code = "not_implemented"


def _envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers that render every error in the shared envelope."""

    @app.exception_handler(AppError)
    async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details),
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_envelope(
                "validation_error",
                "Request validation failed.",
                {"errors": exc.errors()},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            status.HTTP_401_UNAUTHORIZED: "unauthorized",
            status.HTTP_403_FORBIDDEN: "forbidden",
            status.HTTP_404_NOT_FOUND: "not_found",
            status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
            status.HTTP_409_CONFLICT: "conflict",
        }.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        return JSONResponse(
            status_code=exc.status_code, content=_envelope(code, message)
        )
