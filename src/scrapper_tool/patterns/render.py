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

import os
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from scrapper_tool._challenge import classify_wall
from scrapper_tool._logging import get_logger
from scrapper_tool._urlguard import assert_tier_allowed, check_url, url_guard_enabled
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

# Anchored on the scheme rather than ``**/*`` on purpose. A catch-all pattern
# also matches the internal ``about:blank`` navigation a context performs, and
# aborting *that* strands the browser before it reaches the page — a mistake
# already made and measured once in this repo (see the interception fix in
# ``test_captcha_detection_dom``), where it turned an intermittent failure into
# a deterministic 30s timeout.
_GUARDED_SCHEMES = re.compile(r"^https?://")


async def _install_url_guard(context: Any, url: str) -> bool:
    """Abort page-initiated requests the guard refuses, before they are issued.

    **What this closes.** A rendered page can make the browser fetch anything it
    names — ``<img src>``, ``<iframe src>``, ``fetch()`` from its own scripts.
    Without a route those all reach the network, which turns the render tier
    into an SSRF primitive driven by whoever controls the page. Measured against
    a real Camoufox: a page carrying an ``<img>`` at ``169.254.169.254``, an
    ``<iframe>`` at ``10.0.0.1`` and a ``fetch()`` at ``127.0.0.53`` had all
    three aborted here, and the page still rendered.

    **What it does not close, verified rather than assumed.** Playwright's
    ``route`` does *not* fire for redirect hops on a navigation — the browser's
    network stack follows a ``302`` internally and the handler only ever sees
    the original URL. Instrumented against a local redirector pointing at the
    metadata endpoint: the handler saw the seed and nothing else, and the
    browser went on to attempt the metadata connection itself. So navigation
    redirects remain blind SSRF on this tier, caught only by the cascade's
    post-flight check on the final URL. Closing that properly would mean
    intercepting with ``route.fetch(max_redirects=0)`` and fulfilling each hop
    by hand, which replaces a native browser fetch with a synthesised response —
    not a trade to make blind on the tier whose entire purpose is stealth.

    Deliberately does **not** exempt ``resource_type == "document"``. That
    exemption reads like belt-and-braces and is worse than useless: an
    ``<iframe src>`` *is* a document, so exempting documents waves through
    exactly the third-party frames worth stopping. Same finding as the
    detection-fixture fix.

    Returns whether the route was installed; a backend that exposes no
    ``route`` is reported rather than silently unguarded.
    """
    if not url_guard_enabled():
        return False
    route = getattr(context, "route", None)
    if not callable(route):
        _logger.warning(
            "patterns.render.guard_unavailable",
            url=url,
            detail="browser context exposes no route(); this render is not hop-guarded",
            remedy="the pre-flight and post-flight checks still apply, but a redirect "
            "into private space would be issued by the browser",
        )
        return False

    async def _handle(route_obj: Any, request: Any) -> None:
        verdict = check_url(str(getattr(request, "url", "") or ""))
        if verdict.allowed:
            await route_obj.continue_()
            return
        _logger.warning(
            "patterns.render.request_refused",
            url=getattr(request, "url", None),
            reason=verdict.reason,
        )
        await route_obj.abort()

    await route(_GUARDED_SCHEMES, _handle)
    return True


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
    # The egress this render actually went out on. Defaults to None (direct), so
    # every existing constructor keeps working; the cascade surfaces it so a
    # caller can tell a vendor refusing *them* from a vendor refusing one IP.
    proxy: str | None = None


def _captcha_solving_enabled() -> bool:
    """On by default; ``SCRAPPER_TOOL_RENDER_SOLVE_CAPTCHA=0`` disables."""
    raw = os.environ.get("SCRAPPER_TOOL_RENDER_SOLVE_CAPTCHA")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _try_clear_challenge(
    page: Any, url: str, html: str, status: int, *, final_url: str = ""
) -> str:
    """Attempt to clear a bot wall on a live render, returning the resulting HTML.

    The captcha stack (settle -> checkbox -> slider -> local vision -> paid
    token) already existed but was reachable only from E1 and E2 — the two most
    expensive tiers. That is backwards: the render tier holds a live Playwright
    page, which is everything the solvers need, and it sits *below* the LLM
    tiers. A wall that a checkbox click would have cleared was escalating into an
    agent loop, or being reported as blocked outright.

    Deliberately gated on an already-detected interstitial. ``solve_on_page``
    will happily settle-and-poll a page with no challenge on it, and paying that
    on every ordinary render would tax the common case for the rare one.

    Never raises. A captcha attempt is an optimisation on top of a render that
    has already happened; if it fails, the caller still gets the walled HTML and
    the cascade escalates exactly as before.
    """
    # Gated on a *detected* challenge, but detection now includes the redirect
    # signal. Without it this gate silently disabled the whole solver on any wall
    # carrying no vendor signature — which is precisely the wall that most needed
    # solving, and the reason the reported captcha page was never even attempted.
    # One verdict, so this gate can never fall behind the detectors again. Left
    # out of step, it keeps the solver switched off on exactly the pages that
    # most need it -- the coupling reported twice now.
    if not classify_wall(html, status, requested_url=url, final_url=final_url).walled:
        return html
    try:
        from scrapper_tool.agent.backends import get_captcha_solver  # noqa: PLC0415
        from scrapper_tool.agent.backends.captcha_dom import solve_on_page  # noqa: PLC0415
        from scrapper_tool.agent.backends.llm import get_vision_backend  # noqa: PLC0415
        from scrapper_tool.agent.types import AgentConfig  # noqa: PLC0415
    except ImportError:
        return html  # no [llm-agent] extra: nothing to solve with

    try:
        cfg = AgentConfig.from_env()
        solved = await solve_on_page(
            page, get_captcha_solver(cfg), url, vision=await get_vision_backend(cfg)
        )
    except Exception as exc:
        _logger.info("patterns.render.captcha_attempt_failed", url=url, error=str(exc)[:200])
        return html
    if not solved:
        _logger.info("patterns.render.captcha_unsolved", url=url)
        return html
    _logger.info("patterns.render.captcha_cleared", url=url)
    return str(await page.content())


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
    solve_captcha: bool | None = None,
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
    solve_captcha
        Attempt to clear a detected bot wall in-page before returning. Defaults
        to the ``SCRAPPER_TOOL_RENDER_SOLVE_CAPTCHA`` setting (on). Costs nothing
        on a page with no challenge on it.

    Raises
    ------
    ImportError
        If the chosen backend's dependency (or its browser binary) is missing.
    """
    assert_tier_allowed("render", url=url)
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

        # Same rule as the cookie injection below, for the same reason: the
        # navigation that goto() issues is the one that matters, so the route
        # has to be registered before it rather than after.
        await _install_url_guard(context, url)

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
        # Before the cookies are harvested, so a clearance won here flows out on
        # RenderResult and the tiers above inherit it instead of re-fighting the
        # same wall from scratch.
        if solve_captcha if solve_captcha is not None else _captcha_solving_enabled():
            html = await _try_clear_challenge(
                page, url, html, status, final_url=str(getattr(page, "url", url) or url)
            )
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
            # Proxy health on the SAME verdict every other gate uses. This
            # line read `has_real_content(html, status)` and was two detectors
            # behind, so a proxy that walked into a wall was recorded healthy
            # and stayed in rotation to burn the next request too.
            if not classify_wall(html, status, requested_url=url, final_url=final_url).walled:
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
        return RenderResult(
            html=html,
            status=status,
            final_url=final_url,
            cookies=harvested,
            proxy=attempt_proxy,
        )


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
    return RenderResult(html=html, status=status, final_url=final_url, proxy=opts.proxy)


__all__ = [
    "RenderResult",
    "render_html",
]
