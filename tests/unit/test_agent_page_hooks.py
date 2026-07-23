"""Unit tests for ``scrapper_tool.agent.backends.page_hooks``.

The shared page-hook seam that captcha + behavior consumers ride on, for
both E2 (browser-use ``on_step_end``) and E1 (Crawl4AI ``after_goto``).
Contract exercised:

- Each consumer is awaited once per hook invocation with ``(page, url=)``.
- A consumer that raises is logged and swallowed — the other consumers
  still run and the hook never propagates.
- ``get_e2_page`` resolves via ``get_current_page()`` and falls back to
  ``agent_current_page``; returns ``None`` when no session/page.
- ``after_goto`` returns the page and prefers the passed ``url``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from scrapper_tool.agent.backends import page_hooks


class _FakePage:
    def __init__(self, url: str = "https://page.example") -> None:
        self.url = url


class _FakeSession:
    def __init__(self, page: Any, *, via_method: bool = True) -> None:
        self._page = page
        if via_method:
            self.agent_current_page = None
        else:
            self.agent_current_page = page
            self.get_current_page = None  # type: ignore[assignment]

    async def get_current_page(self) -> Any:
        return self._page


class _FakeAgent:
    def __init__(self, session: Any) -> None:
        self.browser_session = session


# --- get_e2_page ----------------------------------------------------------


async def test_get_e2_page_via_get_current_page() -> None:
    page = _FakePage()
    agent = _FakeAgent(_FakeSession(page, via_method=True))
    assert await page_hooks.get_e2_page(agent) is page


async def test_get_e2_page_falls_back_to_attribute() -> None:
    page = _FakePage()
    agent = _FakeAgent(_FakeSession(page, via_method=False))
    assert await page_hooks.get_e2_page(agent) is page


async def test_get_e2_page_none_without_session() -> None:
    class _Bare:
        browser_session = None

    assert await page_hooks.get_e2_page(_Bare()) is None


# --- make_on_step_end -----------------------------------------------------


async def test_on_step_end_runs_all_consumers_with_page_and_url() -> None:
    page = _FakePage("https://step.example")
    agent = _FakeAgent(_FakeSession(page))
    c1 = AsyncMock()
    c2 = AsyncMock()

    hook = page_hooks.make_on_step_end(c1, c2)
    await hook(agent)

    c1.assert_awaited_once_with(page, url="https://step.example")
    c2.assert_awaited_once_with(page, url="https://step.example")


async def test_on_step_end_swallows_consumer_error() -> None:
    page = _FakePage()
    agent = _FakeAgent(_FakeSession(page))
    boom = AsyncMock(side_effect=RuntimeError("solver blew up"))
    ok = AsyncMock()

    hook = page_hooks.make_on_step_end(boom, ok)
    await hook(agent)  # must not raise

    boom.assert_awaited_once()
    ok.assert_awaited_once()  # second consumer still runs


async def test_on_step_end_noop_when_no_page() -> None:
    class _Bare:
        browser_session = None

    c1 = AsyncMock()
    hook = page_hooks.make_on_step_end(c1)
    await hook(_Bare())
    c1.assert_not_awaited()


# --- make_after_goto ------------------------------------------------------


async def test_after_goto_runs_consumers_and_returns_page() -> None:
    page = _FakePage("https://fallback.example")
    c1 = AsyncMock()

    hook = page_hooks.make_after_goto(c1)
    returned = await hook(page, context=object(), url="https://goto.example", response=None)

    assert returned is page
    c1.assert_awaited_once_with(page, url="https://goto.example")


async def test_after_goto_falls_back_to_page_url() -> None:
    page = _FakePage("https://only-on-page.example")
    c1 = AsyncMock()

    hook = page_hooks.make_after_goto(c1)
    await hook(page, url="")

    c1.assert_awaited_once_with(page, url="https://only-on-page.example")
