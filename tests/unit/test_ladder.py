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
        fake_curl.STATUS_FOR_PROFILE = {"chrome133a": 200}
        resp, profile = await request_with_ladder("GET", "https://example.test/ok")
        assert resp.status_code == 200
        assert profile == "chrome133a"
        # Only one session was constructed — we didn't touch the fallbacks.
        assert len(fake_curl.INSTANCES) == 1
        assert fake_curl.INSTANCES[0].impersonate == "chrome133a"


class TestLadderFallback:
    @pytest.mark.asyncio
    async def test_403_then_200_uses_second_profile(self, fake_curl: type[FakeCurlSession]) -> None:
        # chrome133a 403, chrome124 200 — the second profile wins.
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome133a": 403,
            "chrome124": 200,
            "safari18_0": 200,
            "firefox135": 200,
        }
        resp, profile = await request_with_ladder("GET", "https://example.test/fallback")
        assert resp.status_code == 200
        assert profile == "chrome124"
        # Two sessions constructed — chrome133a tried, chrome124 won.
        assert len(fake_curl.INSTANCES) == 2
        assert [s.impersonate for s in fake_curl.INSTANCES] == [
            "chrome133a",
            "chrome124",
        ]

    @pytest.mark.asyncio
    async def test_503_rotates_like_403(self, fake_curl: type[FakeCurlSession]) -> None:
        # 503 from chrome133a → rotate to chrome124 (which 200s).
        # Note: request_with_retry retries 5xx 3 times *within* a profile;
        # the inner exhaustion still returns the 503 response, which
        # the ladder then treats as a rotate signal.
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome133a": 503,
            "chrome124": 200,
            "safari18_0": 200,
            "firefox135": 200,
        }
        resp, profile = await request_with_ladder("GET", "https://example.test/svc-unavail")
        assert resp.status_code == 200
        assert profile == "chrome124"

    @pytest.mark.asyncio
    async def test_safari_wins_when_all_chrome_burned(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome133a": 403,
            "chrome124": 403,
            "safari18_0": 200,
            "firefox135": 200,
        }
        resp, profile = await request_with_ladder("GET", "https://example.test/all-chrome-burned")
        assert resp.status_code == 200
        assert profile == "safari18_0"
        assert len(fake_curl.INSTANCES) == 3


class TestLadderExhaustion:
    @pytest.mark.asyncio
    async def test_all_profiles_403_raises_blocked_error(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome133a": 403,
            "chrome124": 403,
            "safari18_0": 403,
            "firefox135": 403,
        }
        with pytest.raises(BlockedError) as excinfo:
            await request_with_ladder("GET", "https://example.test/blocked")
        # The error message should hint at the next escalation step.
        assert "Pattern D" in str(excinfo.value)
        assert "Scrapling" in str(excinfo.value)
        # All four profiles were tried.
        assert len(fake_curl.INSTANCES) == 4


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
        """The exported default ladder is the documented 4-profile chain."""
        assert IMPERSONATE_LADDER == (
            "chrome133a",
            "chrome124",
            "safari18_0",
            "firefox135",
        )


class TestLadderHeaderMerging:
    @pytest.mark.asyncio
    async def test_extra_headers_propagate_to_each_session(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome133a": 403, "chrome124": 200}
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
            "chrome133a": 403,
            "chrome124": 403,
            "safari18_0": 200,
        }
        pool = ProxyPool.from_urls(["http://p1:1", "http://p2:2", "http://p3:3"])

        resp, profile = await request_with_ladder("GET", "https://example.test/p", proxy_pool=pool)
        assert resp.status_code == 200
        assert profile == "safari18_0"

        used = [inst.proxy for inst in fake_curl.INSTANCES]
        assert used == ["http://p1:1", "http://p2:2", "http://p3:3"], (
            "each ladder rung must get a fresh IP, not reuse the burned one"
        )

    @pytest.mark.asyncio
    async def test_blocked_proxies_are_penalised_and_winner_marked_ok(
        self, fake_curl: type[FakeCurlSession]
    ) -> None:
        from scrapper_tool.proxy import ProxyPool

        fake_curl.STATUS_FOR_PROFILE = {"chrome133a": 403, "chrome124": 200}
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

        fake_curl.STATUS_FOR_PROFILE = {"chrome133a": 200}
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
        fake_curl.STATUS_FOR_PROFILE = {"chrome133a": 200}
        resp, _ = await request_with_ladder("GET", "https://example.test/p")
        assert resp.status_code == 200
        assert fake_curl.INSTANCES[0].proxy is None
