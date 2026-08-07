"""The trailing-slash redirect must not downgrade the scheme (D-15).

Fly Proxy terminates TLS and forwards over the private 6PN network, so the ASGI scope
says `scheme=http`. Starlette's trailing-slash redirect builds an ABSOLUTE Location from
that scope, so `GET https://…/api/v1/me/` answered `307 Location: http://…/api/v1/me`.
curl follows it happily; a browser on an HTTPS page refuses it as mixed content. Verified
on production before the fix.

The register called this "a one-line proxy-header setting", which was almost right and
would not have worked: uvicorn already mounts ProxyHeadersMiddleware by default, but
trusts only FORWARDED_ALLOW_IPS (`127.0.0.1`), and the 6PN peer is not that. Passing
`--proxy-headers` would have changed nothing. Mounting the middleware in the app with
`trusted_hosts="*"` is what actually fixes it — and puts it under test, which a deploy
flag could never be.

The redirect itself is unchanged and still worth avoiding: it costs a round trip and,
per §3.1.6, happens before authentication. Build URLs without trailing slashes.
"""

from __future__ import annotations

import pytest

_HTTPS = {"X-Forwarded-Proto": "https"}


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/v1/categories/", id="api-collection"),
        pytest.param("/api/v1/notifications/", id="api-notifications"),
        pytest.param("/health/", id="health"),
    ],
)
async def test_trailing_slash_redirect_honours_forwarded_proto(anon_client, path):
    resp = await anon_client.get(path, headers=_HTTPS)

    assert resp.status_code == 307, resp.text
    assert resp.headers["location"].startswith("https://"), resp.headers["location"]


async def test_the_redirect_stays_http_without_the_header(anon_client):
    """Keeps the test above honest: it passes because of the header, not by accident."""
    resp = await anon_client.get("/api/v1/categories/")

    assert resp.status_code == 307, resp.text
    assert resp.headers["location"].startswith("http://")


async def test_a_nonsense_forwarded_proto_is_ignored(anon_client):
    """uvicorn allow-lists the scheme; a client cannot redirect us anywhere it likes."""
    resp = await anon_client.get(
        "/api/v1/categories/", headers={"X-Forwarded-Proto": "gopher"}
    )

    assert resp.status_code == 307, resp.text
    assert resp.headers["location"].startswith("http://")


async def test_the_redirect_still_precedes_authentication(anon_client):
    """Documented in §3.1.4 and unchanged by this fix — pinned so it stays documented.

    An unauthenticated caller gets the 307, not a 401: the router redirects before any
    dependency runs. That is why the frontend must not rely on the redirect.
    """
    resp = await anon_client.get("/api/v1/categories/", headers=_HTTPS)

    assert resp.status_code == 307, resp.text
