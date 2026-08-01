"""Stealth-browser render tier — rendered HTML with NO LLM.

The missing seam. The backends in :mod:`scrapper_tool.agent.backends.browser`
(Camoufox / Patchright / Obscura / Scrapling) already exist, but every consumer
drove them through an LLM (Crawl4AI in E1, browser-use in E2). There was no
"launch → navigate → give me the HTML" helper.

That gap mattered: on a Radware-protected target, a *single* Camoufox navigation
plus a settle returned the fully-rendered page, while the cheap HTTP tier got a
challenge loader and the LLM tiers hit captchas. Rendering + the existing
deterministic extractors (Pattern B/C/CSS) is therefore both cheaper *and* more
reliable than escalating to an LLM — this module is what makes that tier
possible.

Contract mirrors :func:`scrapper_tool.patterns.d.hostile_client`'s response shape
(``html`` / ``status`` / ``final_url``) so cascade steps built on Pattern D can
reuse the same extraction + classification code path.

Usage::

    from scrapper_tool.patterns.render import render_html

    result = await render_html("https://example.com", browser="camoufox")
    # then feed result.html to the Pattern B/C/CSS extractors
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from scrapper_tool._challenge import has_real_content
from scrapper_tool._logging import get_logger
from scrapper_tool.agent.backends.browser import (
    BrowserLaunchOptions,
    get_browser_backend,
    open_browser,
    resolve_context,
)
from scrapper_tool.cookies import cookies_for_url, redact, to_playwright
from scrapper_tool.proxy import resolve_proxy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scrapper_tool.cookies import CookieIn
    from scrapper_tool.proxy import ProxyPool

_logger = get_logger(__name__)

# A short settle after load lets JS challenge interstitials clear themselves and
# late/lazy content arrive. This is what made a plain Camoufox navigation pass a
# Radware wall where a bare fetch did not.
_DEFAULT_SETTLE_S = 2.0


@dataclass(frozen=True)
class RenderResult:
    """Outcome of a stealth-browser render.

    Mirrors the ``(html, status, url)`` shape Pattern D's fetcher yields so
    cascade steps can treat both interchangeably.
    """

    html: str
    status: int
    final_url: str
    cookies: Sequence[dict[str, Any]] = field(default_factory=tuple)


async def render_html(
    url: str,
    *,
    browser: str = "camoufox",
    timeout_s: float = 45.0,
    network_idle: bool = True,
    settle_s: float = _DEFAULT_SETTLE_S,
    options: BrowserLaunchOptions | None = None,
    cdp_url: str | None = None,
    fingerprint: str = "browserforge",
    behavior: str = "off",
    proxy_pool: ProxyPool | None = None,
    cookies: list[CookieIn] | None = None,
) -> RenderResult:
    """Render ``url`` with a stealth browser and return the HTML. No LLM.

    Parameters
    ----------
    browser
        Backend name — ``camoufox`` (best stealth), ``patchright``, ``obscura``
        (lightweight CDP), or ``scrapling`` (delegates to Pattern D's fetcher).
    network_idle
        Wait for the network to settle rather than just DOM-ready. Needed for
        SPA-rendered content (most modern listing pages).
    settle_s
        Extra dwell after load — lets JS challenges clear and lazy content load.
    options
        :class:`BrowserLaunchOptions`; carries ``user_data_dir`` (persistent
        profile, so clearance cookies survive), proxy, and the render knobs.
    cdp_url
        CDP endpoint for the Obscura backend.
    cookies
        Caller-supplied cookies. Only those matching ``url`` by domain, path,
        scheme and expiry are injected, and injection happens *before* the
        first navigation.

    Raises
    ------
    ImportError
        If the chosen backend's dependency (or its browser binary) is missing.
    """
    opts = options or BrowserLaunchOptions()
    # Browser tiers need the IP-reputation dimension too — a stealth browser on a
    # burned IP still gets walled. Only consult the pool when no proxy was pinned.
    attempt_proxy, managed_pool = resolve_proxy(proxy_pool, opts.proxy)
    if attempt_proxy != opts.proxy:
        opts = replace(opts, proxy=attempt_proxy)
    backend = get_browser_backend(browser, cdp_url=cdp_url)

    # Scrapling's fetcher is HTTP-shaped (no Playwright Browser), so it can't be
    # driven page-wise — delegate to Pattern D, which already wraps it.
    if browser == "scrapling":
        return await _render_via_scrapling(url, timeout_s=timeout_s, opts=opts)

    from scrapper_tool.agent.backends import (  # noqa: PLC0415
        get_behavior_policy,
        get_fingerprint_generator,
    )

    wait_until = "networkidle" if network_idle else "domcontentloaded"
    async with open_browser(
        backend,
        options=opts,
        fingerprint=get_fingerprint_generator(fingerprint),
        behavior=get_behavior_policy(behavior),
    ) as handle:
        pw_browser = handle.playwright_browser
        if pw_browser is None:  # pragma: no cover — only scrapling, handled above
            msg = f"browser backend {handle.name!r} exposes no Playwright Browser to render with"
            raise ImportError(msg)

        context = await resolve_context(pw_browser)

        # Inject before the first navigation, never after. The request that
        # decides logged-in vs logged-out is the one goto() issues, so an
        # after-goto hook would fetch the logged-out page and then helpfully
        # attach the session to nothing. Letting add_cookies raise is also
        # deliberate: _do_render_step catches and logs tier exceptions properly,
        # whereas the page-hook consumer path swallows them, which would make a
        # failed injection invisible.
        if cookies:
            applicable = cookies_for_url(cookies, url)
            if applicable:
                await context.add_cookies(to_playwright(applicable))
                _logger.debug(
                    "patterns.render.cookies_applied",
                    url=url,
                    cookies=redact(applicable),
                )

        page = context.pages[0] if getattr(context, "pages", None) else await context.new_page()

        response = await page.goto(url, wait_until=wait_until, timeout=timeout_s * 1000)
        if settle_s > 0:
            await page.wait_for_timeout(settle_s * 1000)

        html = await page.content()
        status = int(getattr(response, "status", 200) or 200)
        final_url = str(getattr(page, "url", url) or url)
        # Named `harvested` rather than `cookies` to keep it distinct from the
        # caller-supplied `cookies` parameter above. These are what the render
        # *won* — a cf_clearance, say — and they flow out on RenderResult.
        harvested: Sequence[dict[str, Any]] = ()
        get_cookies = getattr(context, "cookies", None)
        if callable(get_cookies):
            try:
                harvested = tuple(await get_cookies())
            except Exception as exc:  # pragma: no cover — defensive
                _logger.debug("patterns.render.cookies_failed", error=str(exc))

        # Feed IP health back to the pool based on CONTENT, not status.
        #
        # Status is not a success signal for a rendered page: store.mopar.com
        # returns HTTP 403 while serving 1.35 MB of genuine DOM (the anti-bot 403s
        # the document, then JS clears the challenge and the real page renders).
        # Penalising the proxy there would poison the pool with false blocks on
        # every successful render. Conversely a bot-walled 200 is a failure.
        if managed_pool is not None:
            if has_real_content(html, status):
                managed_pool.mark_ok(attempt_proxy)
            else:
                managed_pool.mark_blocked(attempt_proxy)

        _logger.info(
            "patterns.render.rendered",
            backend=handle.name,
            url=url,
            final_url=final_url,
            status=status,
            bytes=len(html),
            proxied=attempt_proxy is not None,
        )
        return RenderResult(html=html, status=status, final_url=final_url, cookies=harvested)


async def _render_via_scrapling(
    url: str, *, timeout_s: float, opts: BrowserLaunchOptions
) -> RenderResult:
    """Render through Pattern D's Scrapling fetcher (HTTP-shaped, no page API)."""
    from scrapper_tool.patterns.d import hostile_client  # noqa: PLC0415

    fetch_kwargs: dict[str, Any] = {"solve_cloudflare": True, "network_idle": True}
    if opts.user_data_dir:
        fetch_kwargs["user_data_dir"] = opts.user_data_dir

    async with hostile_client(timeout=timeout_s, headless=not opts.headful) as fetcher:
        response = await fetcher.async_fetch(url, **fetch_kwargs)

    html = getattr(response, "html_content", None) or getattr(response, "body", None) or ""
    status = int(getattr(response, "status", None) or getattr(response, "status_code", 0) or 0)
    final_url = str(getattr(response, "url", url) or url)
    _logger.info(
        "patterns.render.rendered",
        backend="scrapling",
        url=url,
        final_url=final_url,
        status=status,
        bytes=len(html),
    )
    return RenderResult(html=html, status=status, final_url=final_url)


__all__ = [
    "RenderResult",
    "render_html",
]
