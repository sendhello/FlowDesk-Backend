"""Validation errors that used to be plain-text 500s (D-13, D-14).

Both defects were the same shape: `exc.errors()` contained something `JSONResponse` could
not serialise, so the failure happened INSIDE the RequestValidationError handler. Nothing
caught it, and the caller got Starlette's default 500 — for a request whose only sin was a
missing header or a `NaN` literal.

Three distinct offenders, only two of which were on the register:

* `input` holding the raw `bytes` body, when Content-Type is absent or is not JSON (D-13).
  FastAPI hands pydantic the unparsed body on purpose: `strict_content_type` defaults to
  True as CSRF hardening, because a browser can send a body with no Content-Type and skip
  the CORS preflight. So this path stays, and 422 is the correct answer for it.
* `input` holding a non-finite float, because `json.loads` accepts the `NaN`/`Infinity`
  literals while `JSONResponse` dumps with `allow_nan=False` (D-14).
* a binary body, which `jsonable_encoder`'s own strict `.decode()` then choked on. Found
  while fixing the first two; it survived a partial fix.

`POST /organizations` throughout: it is public, so no auth is needed, and it is the
endpoint where all three were reported.
"""

from __future__ import annotations

import pytest

_VALID_JSON = b'{"organization_name":"Acme","admin_email":"a@b.com","admin_name":"Admin"}'
_JSON_CT = {"Content-Type": "application/json"}


def _errors(resp) -> list[dict]:
    body = resp.json()
    assert body["error"]["code"] == "validation_error", body
    return body["error"]["details"]["errors"]


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(None, id="absent"),
        pytest.param({"Content-Type": "text/plain"}, id="text-plain"),
        pytest.param(
            {"Content-Type": "application/x-www-form-urlencoded"}, id="form-urlencoded"
        ),
    ],
)
async def test_json_body_with_a_bad_content_type_returns_422(anon_client, headers):
    """D-13. Was a plain-text 500 that a browser reported as a CORS failure."""
    resp = await anon_client.post(
        "/api/v1/organizations", content=_VALID_JSON, headers=headers
    )

    assert resp.status_code == 422, resp.text
    assert _errors(resp)[0]["type"] == "model_attributes_type"


async def test_binary_body_returns_422(anon_client):
    """The third offender: a body that is not valid UTF-8 at all."""
    resp = await anon_client.post("/api/v1/organizations", content=b"\xff\xfe\x00binary")

    assert resp.status_code == 422, resp.text
    assert _errors(resp)[0]["type"] == "model_attributes_type"


@pytest.mark.parametrize(
    ("literal", "expected"),
    [(b"NaN", "nan"), (b"Infinity", "inf"), (b"-Infinity", "-inf")],
)
async def test_non_finite_float_literal_returns_422(anon_client, literal, expected):
    """D-14. `json.loads` accepts these; `JSONResponse` refuses to write them back."""
    body = b'{"organization_name":' + literal + b',"admin_email":"a@b.com","admin_name":"A"}'
    resp = await anon_client.post("/api/v1/organizations", content=body, headers=_JSON_CT)

    assert resp.status_code == 422, resp.text
    error = _errors(resp)[0]
    assert error["loc"] == ["body", "organization_name"]
    assert error["input"] == expected


async def test_non_finite_float_nested_in_a_list_returns_422(anon_client):
    """Proves the encoder recurses: here the float is inside `input`, not `input` itself."""
    resp = await anon_client.post(
        "/api/v1/organizations", content=b"[-Infinity, 1.5]", headers=_JSON_CT
    )

    assert resp.status_code == 422, resp.text
    # Finite floats survive as floats — the encoder rewrites only what cannot be written.
    assert _errors(resp)[0]["input"] == ["-inf", 1.5]


async def test_malformed_json_still_returns_422(anon_client):
    """Regression guard on the one malformed-body case that always worked."""
    resp = await anon_client.post(
        "/api/v1/organizations", content=b'{"a":', headers=_JSON_CT
    )

    assert resp.status_code == 422, resp.text
    assert _errors(resp)[0]["type"] == "json_invalid"


async def test_the_documented_error_shape_is_unchanged(anon_client):
    """Brad parses these. Fixing serialisation must not quietly reshape them."""
    resp = await anon_client.post(
        "/api/v1/organizations",
        json={"organization_name": "Acme", "admin_email": "nope", "admin_name": "A"},
    )

    assert resp.status_code == 422, resp.text
    error = _errors(resp)[0]
    assert {"type", "loc", "msg", "input", "ctx"} <= set(error)
    assert error["loc"] == ["body", "admin_email"]
    assert error["input"] == "nope"


async def test_an_oversized_body_is_not_echoed_whole(anon_client):
    """A public endpoint must not amplify: 200 KB in used to mean 600 KB out.

    pydantic repeats the entire parsed body as `input` on every `missing` error, so
    capping the raw `bytes` case alone left the amplification wide open whenever the
    caller sent a correct Content-Type.
    """
    body = b'{"organization_name":"' + b"x" * 200_000 + b'"}'
    resp = await anon_client.post("/api/v1/organizations", content=body, headers=_JSON_CT)

    assert resp.status_code == 422, resp.text
    assert len(resp.content) < 4096, len(resp.content)
    assert _errors(resp)[0]["input"].endswith("…[truncated]")


async def test_a_bad_content_type_creates_no_organisation(client, db, fake_supabase):
    """The request must die in validation, before the saga starts.

    Mirrors test_supabase_failures.test_rate_limited_registration_creates_nothing: a 422 is
    only correct if it also means nothing happened.
    """
    from sqlalchemy import func, select

    from app.models.tenant import Tenant

    resp = await client.post("/api/v1/organizations", content=_VALID_JSON)

    assert resp.status_code == 422, resp.text
    assert int(await db.scalar(select(func.count()).select_from(Tenant)) or 0) == 0
    assert fake_supabase.invited == []
