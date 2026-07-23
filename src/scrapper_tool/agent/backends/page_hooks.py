"""Shared page-hook plumbing for Pattern E (E1 + E2).

Both the captcha solver and the behavior policy need the same thing: the
live Playwright :class:`Page` mid-loop, in both agent modes. E2
(browser-use) exposes it through an ``on_step_end`` lifecycle hook that
receives the ``Agent``; E1 (Crawl4AI) exposes it through an
``after_goto`` strategy hook that receives the ``page`` directly. This
module builds one *consumer* abstraction and two thin hook adapters so
the captcha/behavior logic is written once and reused across both modes.

A **consumer** is ``async def (page, *, url: str) -> None`` — a unit of
per-page work (check for a captcha, apply behavior shaping). Consumers
are intentionally decoupled from browser-use / Crawl4AI internals; only
the adapters here know how to fish the page out of each framework.

Consumer errors are logged and swallowed: a solver hiccup or a behavior
timing glitch must never abort the agent loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scrapper_tool._logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    # (page, *, url) -> None
    PageConsumer = Callable[..., Awaitable[None]]

_logger = get_logger(__name__)


async def get_e2_page(agent: Any) -> Any | None:
    """Return the live Playwright page from a browser-use ``Agent``.

    browser-use 0.5.x exposes it via ``agent.browser_session`` — either
    ``await session.get_current_page()`` or the ``agent_current_page``
    attribute. Both are tried so a minor API bump doesn't silently drop
    the hook. Returns ``None`` when no page is reachable.
    """
    session = getattr(agent, "browser_session", None)
    if session is None:
        return None
    get_page = getattr(session, "get_current_page", None)
    if callable(get_page):
        try:
            page = await get_page()
        except Exception as exc:  # pragma: no cover — defensive against API drift
            _logger.debug("agent.page_hook.get_current_page_failed", error=str(exc))
            page = None
        if page is not None:
            return page
    return getattr(session, "agent_current_page", None)


async def _run_consumers(consumers: tuple[PageConsumer, ...], page: Any, url: str) -> None:
    for consumer in consumers:
        try:
            await consumer(page, url=url)
        except Exception as exc:
            # A consumer failure (solver error, behavior glitch) must not
            # abort the agent loop — log and continue.
            _logger.warning(
                "agent.page_hook.consumer_failed",
                consumer=getattr(consumer, "__name__", repr(consumer)),
                error=str(exc),
            )


def make_on_step_end(*consumers: PageConsumer) -> Callable[[Any], Awaitable[None]]:
    """Build a browser-use ``on_step_end`` hook that runs ``consumers``.

    Signature matches browser-use's ``AgentHookFunc``
    (``Callable[[Agent], Awaitable[None]]``). Resolves the live page once
    per step and feeds each consumer ``(page, url=page.url)``.
    """

    async def on_step_end(agent: Any) -> None:
        page = await get_e2_page(agent)
        if page is None:
            return
        url = str(getattr(page, "url", "") or "")
        await _run_consumers(consumers, page, url)

    return on_step_end


def make_after_goto(*consumers: PageConsumer) -> Callable[..., Awaitable[Any]]:
    """Build a Crawl4AI ``after_goto`` strategy hook that runs ``consumers``.

    Crawl4AI invokes ``after_goto`` as
    ``hook(page, context=..., url=..., response=..., config=..., **kwargs)``
    and uses the returned page. Feeds each consumer ``(page, url=url)`` and
    returns the (possibly mutated) page unchanged.
    """

    async def after_goto(
        page: Any,
        context: Any = None,
        url: str = "",
        response: Any = None,
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        _ = (context, response, config, kwargs)  # accepted, unused
        effective_url = str(url or getattr(page, "url", "") or "")
        await _run_consumers(consumers, page, effective_url)
        return page

    return after_goto


__all__ = [
    "get_e2_page",
    "make_after_goto",
    "make_on_step_end",
]
