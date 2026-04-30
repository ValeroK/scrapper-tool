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
