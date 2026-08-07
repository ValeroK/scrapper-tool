"""CORS configuration — the wildcard-plus-credentials pairing.

The CORS spec forbids `Access-Control-Allow-Origin: *` together with
`Access-Control-Allow-Credentials: true`, and the long-standing assumption was
that the pairing is inert because browsers reject it.

Checked against the pinned Starlette, it is **not** inert: with
``allow_origins=["*"]`` and ``allow_credentials=True``, Starlette reflects the
request's ``Origin`` verbatim *and* sends ``allow-credentials: true``. Any page
on any origin could then make credentialed cross-origin requests to this sidecar
and read the responses — which matters a great deal for a sidecar that holds API
keys and, now, session cookies.

These tests pin the fixed behaviour so a future refactor can't quietly restore
the exploitable pairing.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from scrapper_tool import http_server

ATTACKER = "https://evil.example"


async def _cors_headers(app: Any, origin: str = ATTACKER) -> dict[str, str | None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health", headers={"Origin": origin})
    return {
        "allow_origin": resp.headers.get("access-control-allow-origin"),
        "allow_credentials": resp.headers.get("access-control-allow-credentials"),
    }


class TestWildcardOrigins:
    @pytest.mark.asyncio
    async def test_wildcard_never_grants_credentials(self) -> None:
        """The pairing that lets any origin read credentialed responses."""
        app = http_server._build_app(api_key=None, cors_origins=["*"])
        headers = await _cors_headers(app)
        assert headers["allow_credentials"] != "true"

    @pytest.mark.asyncio
    async def test_wildcard_does_not_reflect_the_attacker_origin(self) -> None:
        app = http_server._build_app(api_key=None, cors_origins=["*"])
        headers = await _cors_headers(app)
        assert headers["allow_origin"] != ATTACKER

    @pytest.mark.asyncio
    async def test_wildcard_still_allows_plain_cross_origin_reads(self) -> None:
        """Anonymous cross-origin use is unaffected — only credentials are dropped."""
        app = http_server._build_app(api_key=None, cors_origins=["*"])
        headers = await _cors_headers(app)
        assert headers["allow_origin"] == "*"


class TestExplicitOrigins:
    @pytest.mark.asyncio
    async def test_an_unlisted_origin_gets_no_grant(self) -> None:
        app = http_server._build_app(api_key=None, cors_origins=["https://app.example.com"])
        headers = await _cors_headers(app)
        assert headers["allow_origin"] != ATTACKER

    @pytest.mark.asyncio
    async def test_a_listed_origin_may_use_credentials(self) -> None:
        """Enumerating origins is how the spec says to do credentialed CORS."""
        app = http_server._build_app(api_key=None, cors_origins=["https://app.example.com"])
        headers = await _cors_headers(app, origin="https://app.example.com")
        assert headers["allow_origin"] == "https://app.example.com"
        assert headers["allow_credentials"] == "true"
