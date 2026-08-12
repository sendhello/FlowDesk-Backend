"""Application error hierarchy and FastAPI exception handlers.

Every non-2xx response uses a single, consistent error envelope so the frontend can
rely on one shape (part of the Ivan <-> Brad API contract):

    {"error": {"code": "<machine_slug>", "message": "<human text>", "details": {...}}}

The envelope is now universal for everything the application answers, including an
unexpected failure: `ErrorEnvelopeMiddleware` is the last resort behind the typed handlers
(D-1). The only response still outside it is a CORS rejection, which is written by
CORSMiddleware before this code is ever reached.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import log_unhandled_exception


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


#: Deliberately says nothing. See `_render_unhandled`.
_UNHANDLED_MESSAGE = "An unexpected internal error occurred. Please try again."


def _render_unhandled(exc: BaseException, *, method: str, path: str) -> JSONResponse:
    """The single renderer for an exception no other handler claimed (D-1).

    Shared by the middleware and the `Exception` handler below so the two cannot drift:
    the `error_id` in the log and the `error_id` in the body are the same string by
    construction rather than by convention.

    The message is a constant, never `str(exc)`. An unhandled exception is the one case
    where nobody has vetted what the string contains — a DSN, a service-role key, a row of
    customer data — and `POST /organizations` is public, so the audience is everyone.
    """
    error_id = uuid.uuid4().hex
    log_unhandled_exception(error_id=error_id, method=method, path=path, exc=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope(
            "internal_error",
            _UNHANDLED_MESSAGE,
            {"reason": "unhandled_exception", "error_id": error_id},
        ),
    )


class ErrorEnvelopeMiddleware:
    """Catch what Starlette's `ExceptionMiddleware` did not, and answer in the envelope.

    Its POSITION is the point. Registering a handler under the `Exception` key instead
    would make it `ServerErrorMiddleware`'s handler — the outermost node, OUTSIDE
    `CORSMiddleware` — so the 500 would carry no `access-control-allow-origin` and a
    browser would still see an opaque CORS failure rather than the error. That is exactly
    how D-13 presented to the frontend for a sprint. `Starlette.add_middleware` inserts at
    index 0, so "inside CORS" means "added BEFORE CORS" in `create_app`.

    Pure ASGI rather than `BaseHTTPMiddleware` for two reasons. `BaseHTTPMiddleware` runs
    the inner app in its own task group and pumps the body through a memory stream, which
    measured ~2.5x the per-request latency of this on every request in the API, for the
    sake of a branch that almost never fires. And it cannot distinguish "failed before the
    response started" from "failed halfway through the body" — the distinction between an
    envelope and a corrupt response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def _send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            if response_started:
                # Status and headers are already on the wire. There is no envelope left to
                # send, so re-raise and let the server tear the connection down rather than
                # append JSON to a half-written body.
                raise
            response = _render_unhandled(
                exc, method=scope.get("method", "-"), path=scope.get("path", "-")
            )
            await response(scope, receive, send)


#: Longest `input` echoed back in a validation error, in characters.
_MAX_INPUT_ECHO = 512

_TRUNCATED_SUFFIX = "…[truncated]"

#: `exc.errors()` is not JSON-safe, and rendering it unguarded is what D-13 and D-14
#: actually were — both crashed INSIDE `_validation_handler`, so the caller got Starlette's
#: plain-text 500 instead of a 422.
#:
#: * `bytes` — FastAPI hands pydantic the raw body when `Content-Type` is absent or is not
#:   JSON (`strict_content_type` defaults to True: it is CSRF hardening, not an oversight),
#:   so `input` is the body itself. `jsonable_encoder`'s own bytes encoder is a strict
#:   `.decode()`, which then raises `UnicodeDecodeError` on a binary body — a third defect
#:   of the same family, hidden behind the first two. Hence the explicit lossy decode.
#: * `float` — `json.loads` accepts the `NaN`/`Infinity` literals, pydantic rejects the
#:   value, and the non-finite float reaches `JSONResponse`, which dumps with
#:   `allow_nan=False`.
_ERROR_ENCODERS: dict[Any, Any] = {
    float: lambda value: value if math.isfinite(value) else str(value),
    bytes: lambda value: value[:_MAX_INPUT_ECHO].decode("utf-8", "replace"),
}


def _cap_echo(value: Any) -> Any:
    """Bound one echoed `input` so a validation error cannot amplify a request.

    `POST /organizations` is public and unauthenticated, and pydantic repeats the whole
    parsed body as `input` on every `missing` error — so a 200 KB body measured a 600 KB
    response before this existed. Capping `bytes` alone was not enough: with a correct
    `Content-Type` the body is parsed, and `input` is then a str or a dict.

    Over the budget the value is replaced by its truncated JSON rendering, so an oversized
    object arrives as a string. That type change is the signal that it was truncated.
    """
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(rendered) <= _MAX_INPUT_ECHO:
        return value
    return rendered[:_MAX_INPUT_ECHO] + _TRUNCATED_SUFFIX


def _safe_errors(errors: Any) -> Any:
    """Render pydantic's error list to something `JSONResponse` can actually serialise."""
    encoded = jsonable_encoder(errors, custom_encoder=_ERROR_ENCODERS)
    if not isinstance(encoded, list):
        return encoded
    return [
        {**err, "input": _cap_echo(err["input"])}
        if isinstance(err, dict) and "input" in err
        else err
        for err in encoded
    ]


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
                {"errors": _safe_errors(exc.errors())},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Render the framework's own errors in the envelope.

        Nothing in `app/` raises `HTTPException` — every application error is an
        `AppError` and goes to the handler above — so everything arriving here was
        produced by Starlette's routing.
        """
        code = {
            status.HTTP_401_UNAUTHORIZED: "unauthorized",
            status.HTTP_403_FORBIDDEN: "forbidden",
            status.HTTP_404_NOT_FOUND: "not_found",
            status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
            status.HTTP_409_CONFLICT: "conflict",
        }.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        # D-5's last gap. A scope 404 ("that category is not yours") and a routing 404
        # ("there is no such URL") shared `code: not_found` and an empty `details`, so they
        # were distinguishable only by message text. Since this handler never sees an
        # application 404, a 404 here is always the routing one.
        details = (
            {"reason": "route_not_found"}
            if exc.status_code == status.HTTP_404_NOT_FOUND
            else None
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(code, message, details),
            # D-8. Starlette already attaches `Allow` to the 405 it raises
            # (starlette.routing.Route.handle); this handler used to drop it on the floor.
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """Backstop for anything raised OUTSIDE `ErrorEnvelopeMiddleware`.

        Registered under `Exception`, so Starlette makes it `ServerErrorMiddleware`'s
        handler: the outermost node in the stack. In practice only `CORSMiddleware` and the
        middleware's own renderer sit there, which is why this cannot be the primary fix —
        its response is written outside CORS and carries no allow-origin header.
        `ServerErrorMiddleware` re-raises after sending, so this path is invisible to the
        default `ASGITransport` used in tests.
        """
        return _render_unhandled(exc, method=request.method, path=request.url.path)
