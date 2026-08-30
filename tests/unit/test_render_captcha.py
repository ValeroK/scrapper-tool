"""The render tier can clear a wall itself.

The captcha stack — settle, checkbox, slider, local vision, paid token — has
existed for releases, but only E1 and E2 ever reached it. That is backwards: the
render tier holds a live Playwright page, which is everything the solvers need,
and it sits *below* the LLM tiers. A wall a checkbox click would have cleared was
escalating into an agent loop, or being reported blocked outright.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool.patterns import render as render_mod

_WALL = "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>"
_CLEARED = "<html><head><title>Real page</title></head><body><p>the goods</p></body></html>"


class _Page:
    """The two things ``_try_clear_challenge`` asks of a page."""

    def __init__(self, after: str) -> None:
        self._after = after
        self.content_calls = 0

    async def content(self) -> str:
        self.content_calls += 1
        return self._after


class TestCaptchaSolvingToggle:
    def test_on_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCRAPPER_TOOL_RENDER_SOLVE_CAPTCHA", raising=False)
        assert render_mod._captcha_solving_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_it_can_be_disabled(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_RENDER_SOLVE_CAPTCHA", raw)
        assert render_mod._captcha_solving_enabled() is False

    def test_an_empty_value_is_not_a_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset-looking env var must not silently turn the tier off."""
        monkeypatch.setenv("SCRAPPER_TOOL_RENDER_SOLVE_CAPTCHA", "   ")
        assert render_mod._captcha_solving_enabled() is True


class TestTryClearChallenge:
    @pytest.mark.asyncio
    async def test_a_clean_page_is_never_touched(self) -> None:
        """The common case must pay nothing.

        ``solve_on_page`` will settle-and-poll a page with no challenge on it, so
        this is gated on an already-detected interstitial rather than run
        speculatively on every render.
        """
        page = _Page(_CLEARED)
        out = await render_mod._try_clear_challenge(page, "https://x.test/", _CLEARED, 200)
        assert out == _CLEARED
        assert page.content_calls == 0

    @pytest.mark.asyncio
    async def test_a_solved_wall_returns_the_freshly_read_dom(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_solve(page: Any, solver: Any, url: str, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr("scrapper_tool.agent.backends.captcha_dom.solve_on_page", fake_solve)
        monkeypatch.setattr(
            "scrapper_tool.agent.backends.llm.get_vision_backend",
            _async_return(None),
        )
        page = _Page(_CLEARED)

        out = await render_mod._try_clear_challenge(page, "https://x.test/", _WALL, 403)

        assert out == _CLEARED
        assert page.content_calls == 1

    @pytest.mark.asyncio
    async def test_an_unsolved_wall_returns_the_original_html(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cascade must still see the wall and escalate normally."""

        async def fake_solve(page: Any, solver: Any, url: str, **kwargs: Any) -> bool:
            return False

        monkeypatch.setattr("scrapper_tool.agent.backends.captcha_dom.solve_on_page", fake_solve)
        monkeypatch.setattr(
            "scrapper_tool.agent.backends.llm.get_vision_backend",
            _async_return(None),
        )
        page = _Page(_CLEARED)

        out = await render_mod._try_clear_challenge(page, "https://x.test/", _WALL, 403)

        assert out == _WALL
        assert page.content_calls == 0

    @pytest.mark.asyncio
    async def test_a_raising_solver_never_fails_the_render(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A solve attempt sits on top of a render that already happened.

        If it explodes the caller must still get the walled HTML — degrading to
        the previous behaviour, never to an exception.
        """

        async def boom(page: Any, solver: Any, url: str, **kwargs: Any) -> bool:
            raise RuntimeError("solver on fire")

        monkeypatch.setattr("scrapper_tool.agent.backends.captcha_dom.solve_on_page", boom)
        monkeypatch.setattr(
            "scrapper_tool.agent.backends.llm.get_vision_backend",
            _async_return(None),
        )

        out = await render_mod._try_clear_challenge(_Page(_CLEARED), "https://x.test/", _WALL, 403)

        assert out == _WALL


def _async_return(value: Any) -> Any:
    async def _inner(*_: Any, **__: Any) -> Any:
        return value

    return _inner


class TestRedirectOpensTheGate:
    """The connection between this release and 3.2.0's captcha solver.

    The solver is gated on a *detected* challenge. A wall carrying no vendor
    signature was invisible to that gate, so the tier that could have cleared it
    was never even invoked -- the reported bug and the new feature failed from one
    root cause.
    """

    @pytest.mark.asyncio
    async def test_a_signature_less_wall_reached_via_redirect_is_solved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captcha = (
            "<html><head><title>Verification</title></head><body>"
            "<h1>Please confirm you are not a robot</h1></body></html>"
        )
        # No vendor signature, so the body-only classifier sees nothing.
        from scrapper_tool._challenge import is_interstitial

        assert is_interstitial(captcha, 200) is None

        called: list[str] = []

        async def fake_solve(page: Any, solver: Any, url: str, **kwargs: Any) -> bool:
            called.append(url)
            return True

        monkeypatch.setattr("scrapper_tool.agent.backends.captcha_dom.solve_on_page", fake_solve)
        monkeypatch.setattr(
            "scrapper_tool.agent.backends.llm.get_vision_backend", _async_return(None)
        )

        out = await render_mod._try_clear_challenge(
            _Page(_CLEARED),
            "https://vendor.test/parts/1",
            captcha,
            200,
            final_url="https://vendor.test/captcha.html",
        )

        assert called, "the solver was never invoked on a redirect-detected wall"
        assert out == _CLEARED

    @pytest.mark.asyncio
    async def test_a_clean_page_with_no_redirect_still_pays_nothing(self) -> None:
        page = _Page(_CLEARED)
        out = await render_mod._try_clear_challenge(
            page, "https://vendor.test/p", _CLEARED, 200, final_url="https://vendor.test/p"
        )
        assert out == _CLEARED
        assert page.content_calls == 0
