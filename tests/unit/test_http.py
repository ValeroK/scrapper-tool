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


class TestGuardedTransport:
    """The URL guard must vet every hop without disturbing httpx's own routing.

    Regression cover for a real bug: the guard was first installed by passing
    ``transport=`` at client construction, which makes httpx skip building its
    proxy ``mounts`` entirely — silently disabling the standard HTTP_PROXY /
    HTTPS_PROXY / NO_PROXY variables that ``trust_env`` honours by default.
    Traffic that used to go through a proxy would have quietly gone direct, and
    no mocked test could see it because respx patches underneath that layer.
    """

    @pytest.mark.asyncio
    async def test_env_proxy_is_still_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
        async with vendor_client() as client:
            assert client._mounts, (
                "env-var proxies stopped being resolved — the guard must wrap "
                "httpx's transports, not replace them"
            )

    @pytest.mark.asyncio
    async def test_every_route_is_guarded_including_the_proxied_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.http import _GuardedTransport

        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
        monkeypatch.setenv("NO_PROXY", "direct.example.test")
        async with vendor_client() as client:
            for url in ("https://example.test/x", "https://direct.example.test/x"):
                transport = client._transport_for_url(httpx.URL(url))
                assert isinstance(transport, _GuardedTransport), f"{url} routed unguarded"

    @pytest.mark.asyncio
    async def test_a_redirect_into_private_space_is_refused_mid_chain(
        self, respx_mock: respx.Router
    ) -> None:
        """The hop the caller never named is the one a call-site check misses."""
        from scrapper_tool.errors import UrlNotAllowed

        respx_mock.get("https://example.test/redirect").mock(
            return_value=httpx.Response(302, headers={"Location": "http://169.254.169.254/creds"})
        )
        hop = respx_mock.get("http://169.254.169.254/creds").mock(
            return_value=httpx.Response(200, text="secrets")
        )
        async with vendor_client() as client:
            with pytest.raises(UrlNotAllowed):
                await request_with_retry(client, "GET", "https://example.test/redirect")
        assert hop.call_count == 0, (
            "the second hop was issued — post-flight refusal is not prevention"
        )

    @pytest.mark.asyncio
    async def test_an_ordinary_redirect_still_follows(self, respx_mock: respx.Router) -> None:
        """The guard must be invisible to legitimate redirects."""
        respx_mock.get("https://example.test/old").mock(
            return_value=httpx.Response(301, headers={"Location": "https://example.test/new"})
        )
        respx_mock.get("https://example.test/new").mock(
            return_value=httpx.Response(200, text="arrived")
        )
        async with vendor_client() as client:
            resp = await request_with_retry(client, "GET", "https://example.test/old")
        assert resp.status_code == 200
        assert resp.text == "arrived"


class TestStrictRedirectHopLoop:
    """curl_cffi has no per-hop hook, so under strict mode we drive the chain.

    The point is *prevention*: a redirect into private space must not be issued
    at all. Post-flight refusal only withholds the body, by which time a
    state-changing GET has already happened.
    """

    class _FakeCurlSession:
        """Records every request and replays a scripted redirect chain."""

        def __init__(self, script: dict[str, tuple[int, str | None]]) -> None:
            self.script = script
            self.calls: list[tuple[str, str, dict[str, str]]] = []

        async def request(
            self, method: str, url: str, *, headers: dict[str, str], **kwargs: object
        ) -> httpx.Response:
            assert kwargs.get("allow_redirects") is False, (
                "the loop must turn libcurl's own following off, or hops stay invisible"
            )
            self.calls.append((method, url, dict(headers)))
            status, location = self.script.get(url, (200, None))
            hdrs = {"Location": location} if location else {}
            return httpx.Response(status, headers=hdrs, request=httpx.Request(method, url))

    @pytest.fixture(autouse=True)
    def _strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS", "1")
        monkeypatch.delenv("SCRAPPER_TOOL_URL_GUARD", raising=False)
        monkeypatch.delenv("SCRAPPER_TOOL_URL_GUARD_ALLOW", raising=False)

    @pytest.mark.asyncio
    async def test_private_hop_is_never_issued(self) -> None:
        from scrapper_tool.errors import UrlNotAllowed
        from scrapper_tool.http import _request_guarding_each_hop

        session = self._FakeCurlSession(
            {"https://example.test/r": (302, "http://169.254.169.254/latest/meta-data/")}
        )
        with pytest.raises(UrlNotAllowed):
            await _request_guarding_each_hop(session, "GET", "https://example.test/r", headers={})
        assert [c[1] for c in session.calls] == ["https://example.test/r"], (
            "the metadata hop was issued -- that is blind SSRF, not prevention"
        )

    @pytest.mark.asyncio
    async def test_ordinary_chain_still_completes(self) -> None:
        from scrapper_tool.http import _request_guarding_each_hop

        session = self._FakeCurlSession(
            {
                "https://example.test/a": (301, "https://example.test/b"),
                "https://example.test/b": (302, "https://example.test/c"),
            }
        )
        resp = await _request_guarding_each_hop(
            session, "GET", "https://example.test/a", headers={}
        )
        assert resp.status_code == 200
        assert [c[1] for c in session.calls][-1] == "https://example.test/c"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "expected"), [(301, "GET"), (302, "GET"), (303, "GET")])
    async def test_post_downgrades_to_get(self, status: int, expected: str) -> None:
        from scrapper_tool.http import _request_guarding_each_hop

        session = self._FakeCurlSession(
            {"https://example.test/a": (status, "https://example.test/b")}
        )
        await _request_guarding_each_hop(
            session, "POST", "https://example.test/a", headers={}, json={"x": 1}
        )
        assert session.calls[-1][0] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [307, 308])
    async def test_307_and_308_preserve_the_method(self, status: int) -> None:
        """These two statuses exist precisely to preserve method and body."""
        from scrapper_tool.http import _request_guarding_each_hop

        session = self._FakeCurlSession(
            {"https://example.test/a": (status, "https://example.test/b")}
        )
        await _request_guarding_each_hop(
            session, "POST", "https://example.test/a", headers={}, json={"x": 1}
        )
        assert session.calls[-1][0] == "POST"

    @pytest.mark.asyncio
    async def test_authorization_is_stripped_cross_origin(self) -> None:
        from scrapper_tool.http import _request_guarding_each_hop

        session = self._FakeCurlSession({"https://a.test/x": (302, "https://b.test/y")})
        await _request_guarding_each_hop(
            session, "GET", "https://a.test/x", headers={"Authorization": "Bearer secret"}
        )
        assert "Authorization" in session.calls[0][2]
        assert "Authorization" not in session.calls[1][2], (
            "a bearer token must not follow a redirect to another origin"
        )

    @pytest.mark.asyncio
    async def test_authorization_survives_same_origin(self) -> None:
        from scrapper_tool.http import _request_guarding_each_hop

        session = self._FakeCurlSession({"https://a.test/x": (302, "https://a.test/y")})
        await _request_guarding_each_hop(
            session, "GET", "https://a.test/x", headers={"Authorization": "Bearer secret"}
        )
        assert session.calls[1][2].get("Authorization") == "Bearer secret"

    @pytest.mark.asyncio
    async def test_a_redirect_loop_is_capped(self) -> None:
        from scrapper_tool.errors import VendorHTTPError
        from scrapper_tool.http import _MAX_REDIRECT_HOPS, _request_guarding_each_hop

        session = self._FakeCurlSession({"https://a.test/x": (302, "https://a.test/x")})
        with pytest.raises(VendorHTTPError, match="redirects"):
            await _request_guarding_each_hop(session, "GET", "https://a.test/x", headers={})
        assert len(session.calls) == _MAX_REDIRECT_HOPS

    @pytest.mark.asyncio
    async def test_a_3xx_without_a_location_is_returned_not_followed(self) -> None:
        from scrapper_tool.http import _request_guarding_each_hop

        session = self._FakeCurlSession({"https://a.test/x": (302, None)})
        resp = await _request_guarding_each_hop(session, "GET", "https://a.test/x", headers={})
        assert resp.status_code == 302
        assert len(session.calls) == 1

    def test_the_flag_is_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """It stays off until a canary proves the loop keeps the fingerprint."""
        from scrapper_tool._urlguard import strict_redirects_enabled

        monkeypatch.delenv("SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS", raising=False)
        assert strict_redirects_enabled() is False
