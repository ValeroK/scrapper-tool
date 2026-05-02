"""Unit tests for the captcha solver cascade.

The HTTP solvers (CapSolver, NopeCHA, 2Captcha) are exercised against
mocked httpx clients — these tests must run in the default ``[dev]``
install with no captcha service reachable.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from scrapper_tool.agent.backends.captcha import (
    AutoCascadeSolver,
    CamoufoxAutoSolver,
    CapSolverSolver,
    NopechaSolver,
    NoSolver,
    TheykaSolver,
    TwoCaptchaSolver,
)
from scrapper_tool.errors import CaptchaSolveError


class TestNoSolver:
    @pytest.mark.asyncio
    async def test_solve_raises(self) -> None:
        with pytest.raises(CaptchaSolveError, match="solver is disabled"):
            await NoSolver().solve("turnstile", "0x4AAAAAAA", "https://example.com")


class TestCamoufoxAutoSolver:
    @pytest.mark.asyncio
    async def test_returns_empty_after_settle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def instant(_s: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", instant)
        token = await CamoufoxAutoSolver(settle_s=0.01).solve(
            "turnstile", "0x4", "https://example.com"
        )
        assert token == ""

    def test_only_supports_turnstile(self) -> None:
        assert CamoufoxAutoSolver().supported == frozenset({"turnstile"})


class TestTheykaSolver:
    def test_supports_only_turnstile(self) -> None:
        assert TheykaSolver().supported == frozenset({"turnstile"})

    @pytest.mark.asyncio
    async def test_rejects_non_turnstile_kind(self) -> None:
        with pytest.raises(CaptchaSolveError, match="only handles 'turnstile'"):
            await TheykaSolver().solve("hcaptcha", "abc", "https://e.com")

    @pytest.mark.asyncio
    async def test_helpful_install_error_when_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "turnstile_solver", None)
        with pytest.raises(CaptchaSolveError, match="\\[turnstile-solver\\]"):
            await TheykaSolver().solve("turnstile", "abc", "https://e.com")


class TestCapSolverSolver:
    @pytest.mark.asyncio
    async def test_full_flow_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mock the create-task → poll-result loop.
        responses = iter(
            [
                _MockResp(200, {"errorId": 0, "taskId": "task-42"}),
                _MockResp(200, {"status": "ready", "solution": {"token": "tok-OK"}}),
            ]
        )

        async def fake_post(self: Any, url: str, **_: Any) -> Any:
            return next(responses)

        async def instant(_s: float) -> None:
            return None

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        monkeypatch.setattr("asyncio.sleep", instant)

        solver = CapSolverSolver(api_key="sk_test")
        token = await solver.solve("turnstile", "0x4AAA", "https://example.com")
        assert token == "tok-OK"

    @pytest.mark.asyncio
    async def test_create_task_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_post(self: Any, url: str, **_: Any) -> Any:
            return _MockResp(
                200,
                {"errorId": 7, "errorDescription": "Insufficient funds"},
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with pytest.raises(CaptchaSolveError, match="Insufficient funds"):
            await CapSolverSolver(api_key="sk").solve("turnstile", "0x4", "https://e.com")

    def test_supports_modern_captcha_types(self) -> None:
        s = CapSolverSolver(api_key="x").supported
        for kind in ("turnstile", "hcaptcha", "recaptcha-v2", "recaptcha-v3", "datadome"):
            assert kind in s


class TestNopechaSolver:
    def test_only_supports_a_subset(self) -> None:
        s = NopechaSolver(api_key="x").supported
        assert "turnstile" in s
        assert "datadome" not in s

    @pytest.mark.asyncio
    async def test_full_flow_returns_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        responses = iter(
            [
                _MockResp(200, {"data": "tok-direct"}),
            ]
        )

        async def fake_post(self: Any, url: str, **_: Any) -> Any:
            return next(responses)

        async def instant(_s: float) -> None:
            return None

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        monkeypatch.setattr("asyncio.sleep", instant)

        token = await NopechaSolver(api_key="sk").solve("turnstile", "0x4", "https://e.com")
        assert token == "tok-direct"

    @pytest.mark.asyncio
    async def test_unsupported_kind_raises(self) -> None:
        with pytest.raises(CaptchaSolveError, match="doesn't support"):
            await NopechaSolver(api_key="sk").solve("datadome", "x", "https://e.com")


class TestCapSolverEdgeCases:
    @pytest.mark.asyncio
    async def test_recaptcha_v3_includes_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen_payloads: list[dict[str, Any]] = []
        responses = iter(
            [
                _MockResp(200, {"errorId": 0, "taskId": "tid"}),
                _MockResp(
                    200,
                    {"status": "ready", "solution": {"gRecaptchaResponse": "tok"}},
                ),
            ]
        )

        async def fake_post(self: Any, url: str, **kwargs: Any) -> Any:
            seen_payloads.append(kwargs.get("json") or {})
            return next(responses)

        async def instant(_s: float) -> None:
            return None

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        monkeypatch.setattr("asyncio.sleep", instant)

        token = await CapSolverSolver(api_key="sk").solve(
            "recaptcha-v3",
            "site",
            "https://e.com",
            action="login",
        )
        assert token == "tok"
        first = seen_payloads[0]
        assert first["task"]["pageAction"] == "login"

    @pytest.mark.asyncio
    async def test_processing_then_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        responses = iter(
            [
                _MockResp(200, {"errorId": 0, "taskId": "tid"}),
                _MockResp(200, {"status": "processing"}),
                _MockResp(200, {"status": "ready", "solution": {"token": "okay"}}),
            ]
        )

        async def fake_post(self: Any, url: str, **_: Any) -> Any:
            return next(responses)

        async def instant(_s: float) -> None:
            return None

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        monkeypatch.setattr("asyncio.sleep", instant)

        token = await CapSolverSolver(api_key="sk").solve("turnstile", "site", "https://e.com")
        assert token == "okay"


class TestTwoCaptchaSolver:
    @pytest.mark.asyncio
    async def test_full_flow_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        get_responses = iter(
            [
                _MockResp(200, {"status": 1, "request": "captcha-id-1"}),
                _MockResp(200, {"status": 1, "request": "tok-2c"}),
            ]
        )

        async def fake_get(self: Any, url: str, **_: Any) -> Any:
            return next(get_responses)

        async def instant(_s: float) -> None:
            return None

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        monkeypatch.setattr("asyncio.sleep", instant)

        token = await TwoCaptchaSolver(api_key="sk").solve("turnstile", "0x4", "https://e.com")
        assert token == "tok-2c"


class TestAutoCascade:
    @pytest.mark.asyncio
    async def test_first_tier_success_short_circuits(self) -> None:
        # Tier 0 returns empty token (= success-via-settle).
        cascade = AutoCascadeSolver(tiers=[CamoufoxAutoSolver(settle_s=0)])
        token = await cascade.solve("turnstile", "0x4", "https://e.com")
        assert token == ""

    @pytest.mark.asyncio
    async def test_tier_failure_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def instant(_s: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", instant)

        always_fails = _StubSolver("dud", supported={"turnstile"})
        always_fails._raise = CaptchaSolveError("nope")

        always_succeeds = _StubSolver("good", supported={"turnstile"})
        always_succeeds._token = "tok-good"

        cascade = AutoCascadeSolver(tiers=[always_fails, always_succeeds])
        token = await cascade.solve("turnstile", "0x4", "https://e.com")
        assert token == "tok-good"

    @pytest.mark.asyncio
    async def test_all_tiers_fail_aggregates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def instant(_s: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", instant)

        a = _StubSolver("a", supported={"turnstile"})
        a._raise = CaptchaSolveError("err-a")
        b = _StubSolver("b", supported={"turnstile"})
        b._raise = CaptchaSolveError("err-b")
        cascade = AutoCascadeSolver(tiers=[a, b])
        with pytest.raises(CaptchaSolveError, match="All captcha tiers failed"):
            await cascade.solve("turnstile", "0x4", "https://e.com")

    @pytest.mark.asyncio
    async def test_unsupported_kind_raises(self) -> None:
        # Cascade with only a Turnstile tier; ask for hcaptcha.
        cascade = AutoCascadeSolver(tiers=[CamoufoxAutoSolver()])
        with pytest.raises(CaptchaSolveError, match="No captcha tier handles"):
            await cascade.solve("hcaptcha", "abc", "https://e.com")


# --- Helpers --------------------------------------------------------------


class _MockResp:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://test")
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=req,
                response=httpx.Response(self.status_code, request=req),
            )


class _StubSolver:
    """Minimal CaptchaSolver impl for cascade tests."""

    def __init__(self, name: str, *, supported: set[str]) -> None:
        self.name = name
        self.requires_api_key = False
        self._supported = supported
        self._raise: Exception | None = None
        self._token: str = ""

    @property
    def supported(self) -> frozenset[str]:
        return frozenset(self._supported)

    async def solve(
        self,
        kind: str,
        site_key: str,
        url: str,
        *,
        action: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        if self._raise is not None:
            raise self._raise
        return self._token


_ = AsyncMock  # keep import for type hint clarity
