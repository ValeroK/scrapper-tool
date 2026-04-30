"""Unit tests for ``scrapper_tool.ladder``.

Covers:
- First profile wins → returns immediately with the right profile name.
- First profile 403 → second profile wins (one-shot fallback).
- All profiles 403 → raises :class:`BlockedError`.
- Empty ladder raises :class:`ValueError`.
- Custom ladder is honoured (override of the module default).
- ``request_with_retry``'s 5xx retry within a profile still works
  (5xx inside a profile retries, 5xx-after-exhaustion rotates).

The tests use a minimal inline ``_FakeCurlSession`` (lifted to
``scrapper_tool.testing.FakeCurlSession`` in M6 — same surface, cleaner
location). It's intentionally simple — duck-types just enough of
:class:`curl_cffi.requests.AsyncSession` for ``request_with_retry`` to
work against it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

import pytest

from scrapper_tool import (
    IMPERSONATE_LADDER,
    BlockedError,
    request_with_ladder,
)
from scrapper_tool import ladder as ladder_module


@dataclass
class _FakeResponse:
    """Minimal duck-typed response — exposes ``.status_code`` only.

    Real ``httpx.Response`` objects also have ``.text`` / ``.json()``
    but the ladder logic only inspects ``.status_code``, so the fake
    can be lean. Tests that need the text body should extend this.
    """

    status_code: int
    text: str = ""

    def json(self) -> Any:  # pragma: no cover — placeholder
        return json.loads(self.text) if self.text else None


@dataclass
class _FakeCurlSession:
    """Drop-in mock for ``curl_cffi.requests.AsyncSession``.

    Constructed by the ladder once per profile. The tests pre-register
    a ``status_for_profile`` map keyed on the impersonation string;
    ``.request()`` returns a :class:`_FakeResponse` with the configured
    status. Tracks every call for assertions.

    Tests configure the response map by patching this class directly
    at ``scrapper_tool.ladder._CurlCffiAsyncSession`` — see fixtures.
    """

    timeout: float = 10.0
    headers: dict[str, str] = field(default_factory=dict)
    proxy: str | None = None
    allow_redirects: bool = True
    impersonate: str = ""

    def __post_init__(self) -> None:
        self._calls: list[tuple[str, str]] = []
        # Register this instance with the class-level call log so tests
        # can assert across profiles even though each profile gets a
        # fresh session instance.
        type(self)._INSTANCES.append(self)

    # Class-level state (ClassVar so the dataclass decorator skips it).
    # Tests override _STATUS_FOR_PROFILE before invoking the ladder;
    # _INSTANCES gets one entry per session constructed.
    _STATUS_FOR_PROFILE: ClassVar[dict[str, int]] = {}
    _INSTANCES: ClassVar[list[_FakeCurlSession]] = []

    async def request(
        self,
        method: str,
        url: str,
        **_kwargs: Any,
    ) -> _FakeResponse:
        self._calls.append((method, url))
        status = type(self)._STATUS_FOR_PROFILE.get(self.impersonate, 200)
        return _FakeResponse(status_code=status)

    async def close(self) -> None:
        return None

    @classmethod
    def reset(cls) -> Self:
        cls._STATUS_FOR_PROFILE = {}
        cls._INSTANCES = []
        return cls  # type: ignore[return-value]


@pytest.fixture
def fake_curl(monkeypatch: pytest.MonkeyPatch) -> type[_FakeCurlSession]:
    """Patch the ladder's curl_cffi class to ``_FakeCurlSession``."""
    _FakeCurlSession.reset()
    monkeypatch.setattr(ladder_module, "_CurlCffiAsyncSession", _FakeCurlSession)
    return _FakeCurlSession


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No sleeps during retry-internal tests."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


class TestLadderHappyPath:
    @pytest.mark.asyncio
    async def test_first_profile_wins(self, fake_curl: type[_FakeCurlSession]) -> None:
        fake_curl._STATUS_FOR_PROFILE = {"chrome133a": 200}
        resp, profile = await request_with_ladder("GET", "https://example.test/ok")
        assert resp.status_code == 200
        assert profile == "chrome133a"
        # Only one session was constructed — we didn't touch the fallbacks.
        assert len(fake_curl._INSTANCES) == 1
        assert fake_curl._INSTANCES[0].impersonate == "chrome133a"


class TestLadderFallback:
    @pytest.mark.asyncio
    async def test_403_then_200_uses_second_profile(
        self, fake_curl: type[_FakeCurlSession]
    ) -> None:
        # chrome133a 403, chrome124 200 — the second profile wins.
        fake_curl._STATUS_FOR_PROFILE = {
            "chrome133a": 403,
            "chrome124": 200,
            "safari18_0": 200,
            "firefox135": 200,
        }
        resp, profile = await request_with_ladder("GET", "https://example.test/fallback")
        assert resp.status_code == 200
        assert profile == "chrome124"
        # Two sessions constructed — chrome133a tried, chrome124 won.
        assert len(fake_curl._INSTANCES) == 2
        assert [s.impersonate for s in fake_curl._INSTANCES] == [
            "chrome133a",
            "chrome124",
        ]

    @pytest.mark.asyncio
    async def test_503_rotates_like_403(self, fake_curl: type[_FakeCurlSession]) -> None:
        # 503 from chrome133a → rotate to chrome124 (which 200s).
        # Note: request_with_retry retries 5xx 3 times *within* a profile;
        # the inner exhaustion still returns the 503 response, which
        # the ladder then treats as a rotate signal.
        fake_curl._STATUS_FOR_PROFILE = {
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
        self, fake_curl: type[_FakeCurlSession]
    ) -> None:
        fake_curl._STATUS_FOR_PROFILE = {
            "chrome133a": 403,
            "chrome124": 403,
            "safari18_0": 200,
            "firefox135": 200,
        }
        resp, profile = await request_with_ladder("GET", "https://example.test/all-chrome-burned")
        assert resp.status_code == 200
        assert profile == "safari18_0"
        assert len(fake_curl._INSTANCES) == 3


class TestLadderExhaustion:
    @pytest.mark.asyncio
    async def test_all_profiles_403_raises_blocked_error(
        self, fake_curl: type[_FakeCurlSession]
    ) -> None:
        fake_curl._STATUS_FOR_PROFILE = {
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
        assert len(fake_curl._INSTANCES) == 4


class TestLadderConfiguration:
    @pytest.mark.asyncio
    async def test_custom_ladder_overrides_default(self, fake_curl: type[_FakeCurlSession]) -> None:
        fake_curl._STATUS_FOR_PROFILE = {"chrome142": 200}
        resp, profile = await request_with_ladder(
            "GET",
            "https://example.test/custom",
            ladder=("chrome142",),  # one-element custom ladder
        )
        assert resp.status_code == 200
        assert profile == "chrome142"
        assert len(fake_curl._INSTANCES) == 1

    @pytest.mark.asyncio
    async def test_empty_ladder_raises_value_error(self, fake_curl: type[_FakeCurlSession]) -> None:
        # No need to set _STATUS_FOR_PROFILE — we never get to a session.
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
        self, fake_curl: type[_FakeCurlSession]
    ) -> None:
        fake_curl._STATUS_FOR_PROFILE = {"chrome133a": 403, "chrome124": 200}
        await request_with_ladder(
            "GET",
            "https://example.test/headers",
            extra_headers={"X-Custom": "hello"},
        )
        # Both sessions got the custom header (each profile is a fresh
        # session, so the merge happens per profile).
        assert all(s.headers.get("X-Custom") == "hello" for s in fake_curl._INSTANCES)
        # And the default UA is present too.
        assert all("scrapper-tool" in s.headers["User-Agent"] for s in fake_curl._INSTANCES)
