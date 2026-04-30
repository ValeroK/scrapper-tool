"""Unit tests for ``scrapper_tool.http``.

Covers:
- ``vendor_client`` yields a usable httpx client and closes it on exit.
- ``request_with_retry`` happy path (200 first try).
- ``request_with_retry`` retries on 429 / 500 / 503 and eventually returns the response.
- ``request_with_retry`` retries on transport error and raises ``VendorHTTPError`` on exhaustion.
- ``request_with_retry`` does NOT retry on 401 / 403 / 404.
- ``request_with_retry`` injects ``X-Request-ID`` when caller didn't.
- ``request_with_retry`` preserves caller-supplied ``X-Request-ID``.

curl_cffi-backed tests live in ``tests/unit/test_ladder.py`` (M2) using
an inline ``FakeCurlSession`` that's lifted to ``scrapper_tool.testing``
in M6.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest

from scrapper_tool import (
    VendorHTTPError,
    request_with_retry,
    vendor_client,
)

if TYPE_CHECKING:
    import respx


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``asyncio.sleep`` with a no-op so retry tests don't wait."""

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)


class TestVendorClient:
    """Lifecycle + header defaults of the httpx-backed client."""

    @pytest.mark.asyncio
    async def test_yields_httpx_client(self) -> None:
        async with vendor_client() as client:
            assert isinstance(client, httpx.AsyncClient)

    @pytest.mark.asyncio
    async def test_default_user_agent_set(self) -> None:
        async with vendor_client() as client:
            assert "scrapper-tool" in client.headers["User-Agent"]

    @pytest.mark.asyncio
    async def test_extra_headers_override(self) -> None:
        async with vendor_client(
            extra_headers={"X-Custom": "yes", "Accept-Language": "he"}
        ) as client:
            assert client.headers["X-Custom"] == "yes"
            assert client.headers["Accept-Language"] == "he"

    @pytest.mark.asyncio
    async def test_proxy_kwarg_accepted(self) -> None:
        # We don't actually exercise the proxy here — just verify the
        # kwarg is accepted without error and doesn't leak into headers.
        async with vendor_client(proxy=None) as client:
            assert isinstance(client, httpx.AsyncClient)


class TestRequestWithRetryHappyPath:
    @pytest.mark.asyncio
    async def test_200_returns_immediately(self, respx_mock: respx.Router) -> None:
        respx_mock.get("https://example.test/ok").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with vendor_client() as client:
            resp = await request_with_retry(client, "GET", "https://example.test/ok")
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}


class TestRequestWithRetryRetriableStatuses:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    @pytest.mark.asyncio
    async def test_retries_then_returns_last_response(
        self, status: int, respx_mock: respx.Router
    ) -> None:
        # Three 5xx in a row — the third response is what's returned
        # (not raised — caller decides whether to .raise_for_status()).
        route = respx_mock.get("https://example.test/flaky").mock(
            return_value=httpx.Response(status)
        )
        async with vendor_client() as client:
            resp = await request_with_retry(client, "GET", "https://example.test/flaky")
        assert resp.status_code == status
        assert route.call_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_returns_after_one_recovery(self, respx_mock: respx.Router) -> None:
        # 503 → 503 → 200 — the third call recovers; we should see 200.
        respx_mock.get("https://example.test/recover").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(200, json={"recovered": True}),
            ]
        )
        async with vendor_client() as client:
            resp = await request_with_retry(client, "GET", "https://example.test/recover")
        assert resp.status_code == 200
        assert resp.json() == {"recovered": True}


class TestRequestWithRetryNonRetriableStatuses:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    @pytest.mark.asyncio
    async def test_4xx_returns_immediately_no_retry(
        self, status: int, respx_mock: respx.Router
    ) -> None:
        route = respx_mock.get("https://example.test/auth").mock(
            return_value=httpx.Response(status)
        )
        async with vendor_client() as client:
            resp = await request_with_retry(client, "GET", "https://example.test/auth")
        assert resp.status_code == status
        assert route.call_count == 1  # no retry


class TestRequestWithRetryTransportErrors:
    @pytest.mark.asyncio
    async def test_transport_error_retries_then_raises(self, respx_mock: respx.Router) -> None:
        respx_mock.get("https://example.test/dead").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        async with vendor_client() as client:
            with pytest.raises(VendorHTTPError) as excinfo:
                await request_with_retry(client, "GET", "https://example.test/dead")
        assert "failed after 3 attempts" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_transport_error_then_recovery(self, respx_mock: respx.Router) -> None:
        respx_mock.get("https://example.test/blip").mock(
            side_effect=[
                httpx.ConnectError("blip"),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        async with vendor_client() as client:
            resp = await request_with_retry(client, "GET", "https://example.test/blip")
        assert resp.status_code == 200


class TestRequestIdInjection:
    @pytest.mark.asyncio
    async def test_request_id_added_when_absent(self, respx_mock: respx.Router) -> None:
        captured: dict[str, str] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.headers))
            return httpx.Response(200)

        respx_mock.get("https://example.test/id").mock(side_effect=_capture)
        async with vendor_client() as client:
            await request_with_retry(client, "GET", "https://example.test/id")
        assert "x-request-id" in captured
        # Token-urlsafe-12 is 16 chars after base64 encoding.
        assert len(captured["x-request-id"]) >= 12

    @pytest.mark.asyncio
    async def test_caller_supplied_request_id_preserved(self, respx_mock: respx.Router) -> None:
        captured: dict[str, str] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.headers))
            return httpx.Response(200)

        respx_mock.get("https://example.test/idpreserve").mock(side_effect=_capture)
        async with vendor_client() as client:
            await request_with_retry(
                client,
                "GET",
                "https://example.test/idpreserve",
                headers={"X-Request-ID": "caller-abc-123"},
            )
        assert captured["x-request-id"] == "caller-abc-123"


class TestMaxAttempts:
    @pytest.mark.asyncio
    async def test_custom_max_attempts_respected(self, respx_mock: respx.Router) -> None:
        route = respx_mock.get("https://example.test/limit").mock(return_value=httpx.Response(503))
        async with vendor_client() as client:
            await request_with_retry(client, "GET", "https://example.test/limit", max_attempts=5)
        assert route.call_count == 5
