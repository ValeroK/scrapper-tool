"""Unit tests for ``scrapper_tool.ladder``.

Covers:
- First profile wins → returns immediately with the right profile name.
- First profile 403 → second profile wins (one-shot fallback).
- All profiles 403 → raises :class:`BlockedError`.
- Empty ladder raises :class:`ValueError`.
- Custom ladder is honoured (override of the module default).
- ``request_with_retry``'s 5xx retry within a profile still works
  (5xx inside a profile retries, 5xx-after-exhaustion rotates).

Uses :class:`scrapper_tool.testing.FakeCurlSession` (lifted in M6 from
the inline mock that lived here originally).
"""

from __future__ import annotations

import asyncio

import pytest

from scrapper_tool import (
    IMPERSONATE_LADDER,
    BlockedError,
    request_with_ladder,
)
from scrapper_tool import ladder as ladder_module
from scrapper_tool.testing import FakeCurlSession


@pytest.fixture
def fake_curl(monkeypatch: pytest.MonkeyPatch) -> type[FakeCurlSession]:
    """Patch the ladder's curl_cffi class to ``FakeCurlSession``."""
    FakeCurlSession.reset()
    monkeypatch.setattr(ladder_module, "_CurlCffiAsyncSession", FakeCurlSession)
    return FakeCurlSession


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No sleeps during retry-internal tests."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


class TestLadderHappyPath:
    @pytest.mark.asyncio
    async def test_first_profile_wins(self, fake_curl: type[FakeCurlSession]) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        resp, profile = await request_with_ladder("GET", "https://example.test/ok")
        assert resp.status_code == 200
        assert profile == "chrome146"
        # Only one session was constructed — we didn't touch the fallbacks.
        assert len(fake_curl.INSTANCES) == 1
        assert fake_curl.INSTANCES[0].impersonate == "chrome146"


class TestLadderFallback:
    @pytest.mark.asyncio
    async def test_403_then_200_uses_second_profile(self, fake_curl: type[FakeCurlSession]) -> None:
        # chrome133a 403, chrome124 200 — the second profile wins.
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome146": 403,
            "chrome142": 200,
            "safari260": 200,
            "firefox147": 200,
        }
        resp, profile = await request_with_ladder("GET", "https://example.test/fallback")
        assert resp.status_code == 200
        assert profile == "chrome142"
        # Two sessions constructed — chrome133a tried, chrome124 won.
        assert len(fake_curl.INSTANCES) == 2
        assert [s.impersonate for s in fake_curl.INSTANCES] == [
            "chrome146",
            "chrome142",
        ]

    @pytest.mark.asyncio
    async def test_503_rotates_like_403(self, fake_curl: type[FakeCurlSession]) -> None:
        # 503 from chrome133a → rotate to chrome124 (which 200s).
        # Note: request_with_retry retries 5xx 3 times *within* a profile;
        # the inner exhaustion still returns the 503 response, which
        # the ladder then treats as a rotate signal.
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome146": 503,
            "chrome142": 200,
            "safari260": 200,
            "firefox147": 200,
        }
        resp, profile = await request_with_ladder("GET", "https://example.test/svc-unavail")
        assert resp.status_code == 200
        assert profile == "chrome142"

    @pytest.mark.asyncio
    async def test_safari_wins_when_all_chrome_burned(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome146": 403,
            "chrome142": 403,
            "safari260": 200,
            "firefox147": 200,
        }
        resp, profile = await request_with_ladder("GET", "https://example.test/all-chrome-burned")
        assert resp.status_code == 200
        assert profile == "safari260"
        assert len(fake_curl.INSTANCES) == 3


class TestLadderExhaustion:
    @pytest.mark.asyncio
    async def test_all_profiles_403_raises_blocked_error(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        # Derived from the ladder rather than hardcoded: spelling out four names
        # meant that adding a fifth rung left it un-mocked, so it returned 200
        # and the test stopped testing exhaustion at all.
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        with pytest.raises(BlockedError) as excinfo:
            await request_with_ladder("GET", "https://example.test/blocked")
        # The error message should hint at the next escalation step.
        assert "Pattern D" in str(excinfo.value)
        assert "Scrapling" in str(excinfo.value)
        assert len(fake_curl.INSTANCES) == len(IMPERSONATE_LADDER)


class TestLadderConfiguration:
    @pytest.mark.asyncio
    async def test_custom_ladder_overrides_default(self, fake_curl: type[FakeCurlSession]) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome142": 200}
        resp, profile = await request_with_ladder(
            "GET",
            "https://example.test/custom",
            ladder=("chrome142",),  # one-element custom ladder
        )
        assert resp.status_code == 200
        assert profile == "chrome142"
        assert len(fake_curl.INSTANCES) == 1

    @pytest.mark.asyncio
    async def test_empty_ladder_raises_value_error(self, fake_curl: type[FakeCurlSession]) -> None:
        # No need to set STATUS_FOR_PROFILE — we never get to a session.
        with pytest.raises(ValueError, match="at least one"):
            await request_with_ladder("GET", "https://example.test/empty", ladder=())

    def test_default_ladder_shape(self) -> None:
        """The exported default ladder is the documented chain, in order."""
        assert IMPERSONATE_LADDER == (
            "chrome146",
            "chrome142",
            "safari260",
            "firefox147",
            "chrome133a",
        )

    def test_every_ladder_profile_is_a_real_curl_cffi_target(self) -> None:
        """The drift guard for dependency upgrades.

        curl_cffi retires impersonation targets between releases, and a name it
        no longer knows fails at *request* time — meaning the ladder silently
        loses a rung in production rather than at import. This asserts the whole
        ladder against the installed library's own list.
        """
        import typing

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        supported = set(typing.get_args(BrowserTypeLiteral))
        assert supported, "curl_cffi exposed no target list — check the import path"
        unknown = [p for p in IMPERSONATE_LADDER if p not in supported]
        assert not unknown, f"ladder references targets curl_cffi dropped: {unknown}"

    def test_ladder_leads_with_a_fresh_profile(self) -> None:
        """A stale primary is itself a fingerprint.

        Impersonating a Chrome build nobody runs any more is as identifying as
        sending a python-requests UA. This fails when curl_cffi ships a newer
        Chrome than the one we lead with, which is the prompt to re-benchmark and
        promote — not an automatic bug.
        """
        import re
        import typing

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        versions = [
            int(m.group(1))
            for target in typing.get_args(BrowserTypeLiteral)
            if (m := re.fullmatch(r"chrome(\d+)", str(target)))
        ]
        newest = max(versions)
        leading = int(re.match(r"chrome(\d+)", IMPERSONATE_LADDER[0]).group(1))  # type: ignore[union-attr]
        assert newest - leading <= 4, (
            f"curl_cffi now ships chrome{newest} but the ladder leads with "
            f"chrome{leading} — benchmark the newer target and promote it."
        )


class TestLadderHeaderMerging:
    @pytest.mark.asyncio
    async def test_extra_headers_propagate_to_each_session(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 403, "chrome142": 200}
        await request_with_ladder(
            "GET",
            "https://example.test/headers",
            extra_headers={"X-Custom": "hello"},
        )
        # Both sessions got the custom header (each profile is a fresh
        # session, so the merge happens per profile).
        assert all(s.headers.get("X-Custom") == "hello" for s in fake_curl.INSTANCES)
        # And the default UA is present too.
        assert all("scrapper-tool" in s.headers["User-Agent"] for s in fake_curl.INSTANCES)


class TestLadderProxyRotation:
    """v1.6.0 — the ladder rotates the IP alongside the TLS fingerprint.

    TLS-profile rotation cannot recover a burned IP, so walking every profile
    from one flagged egress address is wasted work. With a pool configured each
    rung must vary both dimensions.
    """

    @pytest.mark.asyncio
    async def test_each_rung_uses_a_different_proxy(self, fake_curl: type[FakeCurlSession]) -> None:
        from scrapper_tool.proxy import ProxyPool

        # First two profiles blocked, third wins.
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome146": 403,
            "chrome142": 403,
            "safari260": 200,
        }
        pool = ProxyPool.from_urls(["http://p1:1", "http://p2:2", "http://p3:3"])

        resp, profile = await request_with_ladder("GET", "https://example.test/p", proxy_pool=pool)
        assert resp.status_code == 200
        assert profile == "safari260"

        used = [inst.proxy for inst in fake_curl.INSTANCES]
        assert used == ["http://p1:1", "http://p2:2", "http://p3:3"], (
            "each ladder rung must get a fresh IP, not reuse the burned one"
        )

    @pytest.mark.asyncio
    async def test_blocked_proxies_are_penalised_and_winner_marked_ok(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        from scrapper_tool.proxy import ProxyPool

        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 403, "chrome142": 200}
        pool = ProxyPool.from_urls(["http://p1:1", "http://p2:2"])

        await request_with_ladder("GET", "https://example.test/p", proxy_pool=pool)

        by_url = {e.url: e for e in pool.entries}
        assert by_url["http://p1:1"].failures == 1  # blocked -> cooling down
        assert by_url["http://p1:1"].cooldown_until > 0
        assert by_url["http://p2:2"].successes == 1  # winner stays hot
        assert by_url["http://p2:2"].failures == 0

    @pytest.mark.asyncio
    async def test_explicit_proxy_overrides_pool_and_is_not_penalised(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        from scrapper_tool.proxy import ProxyPool

        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        pool = ProxyPool.from_urls(["http://pool:1"])

        with pytest.raises(BlockedError):
            await request_with_ladder(
                "GET", "https://example.test/p", proxy="http://pinned:1", proxy_pool=pool
            )

        assert {i.proxy for i in fake_curl.INSTANCES} == {"http://pinned:1"}
        # A caller-pinned proxy isn't pool-managed, so its health is untouched.
        assert pool.entries[0].failures == 0

    @pytest.mark.asyncio
    async def test_exhausted_pool_falls_back_to_direct(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        from scrapper_tool.proxy import ProxyPool

        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        pool = ProxyPool.from_urls(["http://p1:1"])
        pool.mark_blocked("http://p1:1")  # everything cooling down

        resp, _ = await request_with_ladder("GET", "https://example.test/p", proxy_pool=pool)
        assert resp.status_code == 200
        # No proxy available -> direct connection rather than failing outright.
        assert fake_curl.INSTANCES[0].proxy is None

    @pytest.mark.asyncio
    async def test_no_pool_preserves_previous_behaviour(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome146": 200}
        resp, _ = await request_with_ladder("GET", "https://example.test/p")
        assert resp.status_code == 200
        assert fake_curl.INSTANCES[0].proxy is None


class TestLadderProxyTransportFailures:
    """Regression: a dead proxy must not kill the whole ladder walk.

    Found by live-testing against free proxies: they passed an http:// liveness
    probe but could not CONNECT-tunnel TLS, so `request_with_retry` raised
    VendorHTTPError, which propagated out and aborted the walk on rung 1 — and the
    proxy was never even penalised (failures stayed 0).
    """

    @pytest.mark.asyncio
    async def test_dead_proxy_rotates_to_next_rung_and_is_penalised(
        self, fake_curl: type[FakeCurlSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import VendorHTTPError
        from scrapper_tool.proxy import ProxyPool

        pool = ProxyPool.from_urls(["http://dead:1", "http://good:2"])
        calls: list[str | None] = []
        real_retry = ladder_module.request_with_retry

        async def flaky_retry(session, method, url, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(session.proxy)
            if session.proxy == "http://dead:1":
                raise VendorHTTPError("CONNECT tunnel failed")
            return await real_retry(session, method, url, **kwargs)

        monkeypatch.setattr(ladder_module, "request_with_retry", flaky_retry)
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 200)

        resp, profile = await request_with_ladder("GET", "https://example.test/p", proxy_pool=pool)
        assert resp.status_code == 200
        # Rung 1 died on the bad proxy; rung 2 succeeded on the good one.
        assert calls == ["http://dead:1", "http://good:2"]
        assert profile == IMPERSONATE_LADDER[1]

        by_url = {e.url: e for e in pool.entries}
        assert by_url["http://dead:1"].failures == 1, "dead proxy must be penalised"
        assert by_url["http://good:2"].successes == 1

    @pytest.mark.asyncio
    async def test_all_proxies_dead_reports_transport_cause(
        self, fake_curl: type[FakeCurlSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import VendorHTTPError
        from scrapper_tool.proxy import ProxyPool

        pool = ProxyPool.from_urls(["http://dead:1"])

        async def always_dead(*_a, **_k):  # type: ignore[no-untyped-def]
            raise VendorHTTPError("CONNECT tunnel failed")

        monkeypatch.setattr(ladder_module, "request_with_retry", always_dead)

        # Must not claim "all profiles returned 403/503" — that would be misleading
        # when the real cause is an unusable proxy pool.
        with pytest.raises(BlockedError, match="transport layer"):
            await request_with_ladder("GET", "https://example.test/p", proxy_pool=pool)

    @pytest.mark.asyncio
    async def test_without_pool_transport_error_still_propagates(
        self, fake_curl: type[FakeCurlSession], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No pool => original behaviour preserved (VendorHTTPError propagates)."""
        from scrapper_tool.errors import VendorHTTPError

        async def always_dead(*_a, **_k):  # type: ignore[no-untyped-def]
            raise VendorHTTPError("network down")

        monkeypatch.setattr(ladder_module, "request_with_retry", always_dead)
        with pytest.raises(VendorHTTPError):
            await request_with_ladder("GET", "https://example.test/p")
