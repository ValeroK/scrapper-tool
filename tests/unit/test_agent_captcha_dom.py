"""Unit tests for ``scrapper_tool.agent.backends.captcha_dom``.

Covers DOM detection, token injection, and the mechanism-aware
``solve_on_page`` orchestration (stealth-settle first, solver token,
graceful failure) using fake Playwright-shaped pages.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from scrapper_tool.agent.backends import captcha_dom
from scrapper_tool.errors import CaptchaSolveError


class _FakePage:
    """Playwright-shaped fake: ``evaluate`` returns queued values."""

    def __init__(self, detect_results: list[Any]) -> None:
        # Each detect call pops the next result; scroll/inject evals are ignored.
        self._detect_results = list(detect_results)
        self.evaluate = AsyncMock(side_effect=self._evaluate)
        self.wait_for_timeout = AsyncMock()
        self.reload = AsyncMock()

    async def _evaluate(self, js: str, arg: Any = None) -> Any:
        if "querySelector" in js:  # the detect JS
            if self._detect_results:
                return self._detect_results.pop(0)
            return None
        return True  # inject JS / scroll JS


class _SpySolver:
    name = "spy"
    requires_api_key = False

    def __init__(self, token: str = "tok", *, raise_error: bool = False) -> None:
        self.solve = AsyncMock(
            side_effect=CaptchaSolveError("nope") if raise_error else None,
            return_value=None if raise_error else token,
        )

    @property
    def supported(self) -> frozenset[str]:
        return frozenset({"turnstile", "hcaptcha", "recaptcha-v2"})


# --- detect_challenge -----------------------------------------------------


async def test_detect_returns_kind_and_sitekey() -> None:
    page = _FakePage([{"kind": "turnstile", "site_key": "0xABC"}])
    assert await captcha_dom.detect_challenge(page) == ("turnstile", "0xABC")


async def test_detect_none_when_absent() -> None:
    page = _FakePage([None])
    assert await captcha_dom.detect_challenge(page) is None


async def test_detect_none_without_evaluate() -> None:
    class _Bare:
        pass

    assert await captcha_dom.detect_challenge(_Bare()) is None


# --- solve_on_page --------------------------------------------------------


async def test_solve_on_page_no_challenge_skips_solver() -> None:
    page = _FakePage([None])
    solver = _SpySolver()
    assert await captcha_dom.solve_on_page(page, solver, "https://x.example") is False
    solver.solve.assert_not_awaited()


async def test_solve_on_page_cleared_by_settle() -> None:
    # First detect (initial) -> challenge; second detect (after settle) -> None.
    page = _FakePage([{"kind": "turnstile", "site_key": "k"}, None])
    solver = _SpySolver()
    assert await captcha_dom.solve_on_page(page, solver, "https://x.example", settle_s=0) is True
    solver.solve.assert_not_awaited()  # stealth pass, no solver needed


async def test_solve_on_page_invokes_solver_and_injects() -> None:
    # detect: initial challenge, after-settle still present -> solver runs.
    page = _FakePage(
        [
            {"kind": "turnstile", "site_key": "k"},  # initial
            {"kind": "turnstile", "site_key": "k"},  # after settle (persists)
            {"kind": "turnstile", "site_key": "k"},  # after inject (ignored)
        ]
    )
    solver = _SpySolver(token="solved-token")
    handled = await captcha_dom.solve_on_page(page, solver, "https://x.example", settle_s=0)
    assert handled is True
    # ``extra`` is threaded through now — DataDome, AWS WAF, GeeTest and image
    # captchas cannot be solved from (kind, site_key) alone. None here because the
    # stubbed detection returns no extra parameters.
    solver.solve.assert_awaited_once_with("turnstile", "k", "https://x.example", extra=None)
    # inject JS ran with the token
    inject_calls = [c for c in page.evaluate.await_args_list if c.args and c.args[1:]]
    assert any(call.args[1] == ["turnstile", "solved-token"] for call in inject_calls)


async def test_solve_on_page_solver_error_is_swallowed() -> None:
    page = _FakePage([{"kind": "hcaptcha", "site_key": "k"}, {"kind": "hcaptcha", "site_key": "k"}])
    solver = _SpySolver(raise_error=True)
    assert await captcha_dom.solve_on_page(page, solver, "https://x.example", settle_s=0) is False


async def test_solve_on_page_empty_token_reloads() -> None:
    # Solver returns "" (tier-0 settle signal); after reload the challenge clears.
    page = _FakePage(
        [
            {"kind": "turnstile", "site_key": "k"},  # initial
            {"kind": "turnstile", "site_key": "k"},  # after first settle (persists)
            None,  # after reload -> cleared
        ]
    )
    solver = _SpySolver(token="")
    handled = await captcha_dom.solve_on_page(page, solver, "https://x.example", settle_s=0)
    assert handled is True
    page.reload.assert_awaited_once()


# --- make_captcha_consumer ------------------------------------------------


async def test_make_captcha_consumer_drives_solve_on_page() -> None:
    page = _FakePage([{"kind": "turnstile", "site_key": "k"}, None])
    solver = _SpySolver()
    consumer = captcha_dom.make_captcha_consumer(solver)
    await consumer(page, url="https://x.example")  # must not raise
