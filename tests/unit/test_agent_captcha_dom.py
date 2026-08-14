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
    """Playwright-shaped fake: ``evaluate`` returns queued values.

    Dispatch is by object identity against the module's JS constants, not by
    substring. Sniffing for ``"querySelector"`` silently matched the token-read
    JS as well as the detect JS, so a token read popped a detect result and the
    two queues corrupted each other.
    """

    def __init__(self, detect_results: list[Any], tokens: list[str] | None = None) -> None:
        self._detect_results = list(detect_results)
        # Default "" = no token, i.e. unsolved, which is what most of these
        # tests are asserting about.
        self._tokens = list(tokens or [])
        self.evaluate = AsyncMock(side_effect=self._evaluate)
        self.wait_for_timeout = AsyncMock()
        self.reload = AsyncMock()

    async def _evaluate(self, js: str, arg: Any = None) -> Any:
        if js is captcha_dom._DETECT_JS:
            # The final queued state repeats once the queue drains. Returning
            # None on exhaustion would mean "the widget vanished", so a test
            # asserting a challenge *persists* had to guess exactly how many
            # detect calls the implementation makes.
            if len(self._detect_results) > 1:
                return self._detect_results.pop(0)
            return self._detect_results[0] if self._detect_results else None
        if js is captcha_dom._RESPONSE_FIELD_JS:
            return self._tokens.pop(0) if self._tokens else ""
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
    """A token that lands in the response field is a solve."""
    page = _FakePage(
        [
            {"kind": "turnstile", "site_key": "k"},  # initial
            {"kind": "turnstile", "site_key": "k"},  # after settle (persists)
        ],
        # No token during the settle re-check; present once the token is injected.
        tokens=["", "solved-token"],
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


async def test_injected_token_that_never_lands_is_reported_as_unsolved() -> None:
    """The regression that matters most in this file.

    ``solve_on_page`` used to ``return True`` unconditionally after injecting.
    A foreign Turnstile token failing its environment check — the *normal*
    outcome, since the token is bound to the context that requested it — was
    therefore reported as a solve, and the cascade stopped escalating while the
    challenge was still up.
    """
    page = _FakePage(
        [{"kind": "turnstile", "site_key": "k"}, {"kind": "turnstile", "site_key": "k"}],
        tokens=[],  # response field never populates
    )
    solver = _SpySolver(token="rejected-token")
    assert await captcha_dom.solve_on_page(page, solver, "https://x.example", settle_s=0) is False


async def test_solved_widget_that_stays_in_the_dom_counts_as_success() -> None:
    """reCAPTCHA and hCaptcha keep their widget after a solve — it turns green.

    So "is the widget gone" is the wrong success question for exactly the kinds
    the new solver tiers target, and a real success would read as a failure.
    """
    page = _FakePage(
        [{"kind": "recaptcha-v2", "site_key": "k"}],  # still detected afterwards
        tokens=["03AGdBq26..."],  # but the response field is populated
    )
    assert await captcha_dom._is_solved(page, "recaptcha-v2") is True


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


# --- checkbox interaction tier --------------------------------------------


class _FakeFrame:
    def __init__(self, url: str, *, has: tuple[str, ...] = ()) -> None:
        self.url = url
        self._has = has
        self.clicked: list[str] = []

    async def wait_for_selector(self, selector: str, timeout: int = 0) -> Any:
        if selector not in self._has:
            raise RuntimeError(f"no such selector: {selector}")
        frame = self

        class _El:
            @staticmethod
            async def click() -> None:
                frame.clicked.append(selector)

        return _El()


class _FramedPage(_FakePage):
    def __init__(self, frames: list[_FakeFrame], **kw: Any) -> None:
        super().__init__(kw.pop("detect_results", [None]), kw.pop("tokens", None))
        self.frames = frames


def test_anchor_frame_is_preferred_over_the_bframe() -> None:
    """The bframe holds the image grid — clicking there hits a tile, not the box."""
    bframe = _FakeFrame("https://www.google.com/recaptcha/api2/bframe?k=x")
    anchor = _FakeFrame("https://www.google.com/recaptcha/api2/anchor?k=x")
    page = _FramedPage([bframe, anchor])
    assert captcha_dom.find_anchor_frame(page, "recaptcha-v2") is anchor


def test_no_anchor_frame_when_none_matches() -> None:
    page = _FramedPage([_FakeFrame("https://example.com/other")])
    assert captcha_dom.find_anchor_frame(page, "recaptcha-v2") is None


def test_turnstile_has_no_checkbox_frame() -> None:
    """Only reCAPTCHA v2 and hCaptcha have an anchor checkbox to click."""
    page = _FramedPage([_FakeFrame("https://challenges.cloudflare.com/x")])
    assert captcha_dom.find_anchor_frame(page, "turnstile") is None


async def test_checkbox_click_that_mints_a_token_is_a_solve() -> None:
    """The free win: a good fingerprint passes on the click, with no solver call."""
    anchor = _FakeFrame(
        "https://www.google.com/recaptcha/api2/anchor?k=x", has=("#recaptcha-anchor",)
    )
    page = _FramedPage([anchor], tokens=["03AGdBq26..."])
    assert await captcha_dom.click_checkbox(page, "recaptcha-v2", settle_s=1) is True
    assert anchor.clicked == ["#recaptcha-anchor"]


async def test_checkbox_click_without_a_token_is_not_a_solve() -> None:
    """No token means the grid appeared — honest False so the cascade escalates."""
    anchor = _FakeFrame(
        "https://www.google.com/recaptcha/api2/anchor?k=x", has=("#recaptcha-anchor",)
    )
    page = _FramedPage([anchor], tokens=[])
    assert await captcha_dom.click_checkbox(page, "recaptcha-v2", settle_s=1) is False


async def test_checkbox_falls_through_selector_variants() -> None:
    """hCaptcha markup varies; a missing selector must not abort the attempt."""
    anchor = _FakeFrame("https://newassets.hcaptcha.com/captcha/v1/x", has=("#anchor .check",))
    page = _FramedPage([anchor], tokens=["P1_eyJ..."])
    assert await captcha_dom.click_checkbox(page, "hcaptcha", settle_s=1) is True
    assert anchor.clicked == ["#anchor .check"]


async def test_solve_on_page_tries_the_checkbox_before_the_solver() -> None:
    """Ordering is the point: a click is free, a paid solve is not.

    Before this tier existed, reCAPTCHA and hCaptcha skipped tier 0 entirely
    (``CamoufoxAutoSolver.supported`` is ``{"turnstile"}``) and went straight to
    a paid solver — paying for challenges a click would have cleared.
    """
    anchor = _FakeFrame(
        "https://www.google.com/recaptcha/api2/anchor?k=x", has=("#recaptcha-anchor",)
    )
    page = _FramedPage(
        [anchor],
        detect_results=[{"kind": "recaptcha-v2", "site_key": "k"}],
        tokens=["", "03AGdBq26..."],  # unsolved at settle, solved after the click
    )
    solver = _SpySolver()
    assert await captcha_dom.solve_on_page(page, solver, "https://x.example", settle_s=0) is True
    solver.solve.assert_not_awaited()
