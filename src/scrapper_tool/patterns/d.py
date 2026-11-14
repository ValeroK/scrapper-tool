"""Pattern D — Hostile sites (Cloudflare Turnstile, Akamai EVA, Distil, …).

Last-resort path when the M2 anti-bot ladder
(``IMPERSONATE_LADDER``, e.g. ``chrome152 → chrome150 → safari270 → …``) has been
exhausted with all-403 responses. Backed by `Scrapling`_, which ships
a Playwright-based fetcher with built-in Turnstile auto-solve and
behavioural-fingerprint mimicry.

.. _Scrapling: https://github.com/D4Vinci/Scrapling

Cost asymmetry — read this before opting in
-------------------------------------------

Pattern D adds ~400 MB of image bloat (Playwright + Chromium) and is
materially slower per request (browser warm-up). Scrapling is therefore
shipped as an **optional extra**:

.. code-block:: bash

    pip install scrapper-tool[hostile]

Without the extra, importing ``scrapper_tool.patterns.d.hostile_client``
will work, but **calling it raises** :class:`ImportError` with a
helpful install hint. The lazy-import keeps the default install lean.

Decision tree
-------------

Before reaching for Pattern D, confirm:

1. M2's ladder genuinely returns 403 on **every** profile of
   ``IMPERSONATE_LADDER``. Capture the 403/200 matrix in
   your adapter notes — future debuggers need it.
2. The vendor's challenge is a **JS-behavioural** one (Cloudflare
   Turnstile, Akamai sensor-data, Distil) rather than just a stale TLS
   profile. Confirm by inspecting the response body for challenge
   signatures.
3. You're OK adding Playwright to your dependency tree.

Usage
-----

::

    from scrapper_tool.patterns.d import hostile_client

    async with hostile_client() as fetcher:
        # Scrapling's fetcher API — see
        # https://scrapling.readthedocs.io/ for the full surface.
        response = await fetcher.async_fetch(url, solve_cloudflare=True)
        html = response.html_content

When to escalate further
------------------------

If Scrapling itself returns 403 on a vendor, you've hit something
exotic (Akamai EVA cookie that Scrapling can't replay, Kasada, fresh
custom WAF). Document the response in the adapter notes and consider:

- Escalating to a managed-SaaS for that one vendor.
- Dropping the vendor and replacing it with an alternative.

The lib does NOT bundle managed-SaaS clients — that's a consumer
decision, not a lib invariant. See ``docs/research/do-not-adopt.md``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from scrapper_tool._logging import get_logger
from scrapper_tool._urlguard import assert_tier_allowed

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from scrapper_tool.cookies import CookieIn

_logger = get_logger(__name__)


_HOSTILE_NOT_INSTALLED = (
    "Pattern D requires Scrapling. Install via:\n"
    "    pip install scrapper-tool[hostile]\n"
    "(adds Playwright + Chromium ~400 MB; opt-in for hostile vendors only)."
)


class CookieKwargUnsupported(TypeError):
    """Raised when the installed Scrapling rejects the ``cookies`` kwarg.

    A distinct type so the cascade can tell "this Scrapling build cannot carry a
    session" — a reportable, non-fatal fact worth a ``cookies_skipped`` entry —
    apart from "Pattern D blew up", which is a tier failure. Subclasses
    ``TypeError`` so any caller that was already catching that keeps working.
    """


@asynccontextmanager
async def hostile_client(
    *,
    timeout: float = 30.0,  # noqa: ASYNC109 — passed to Scrapling, not asyncio.timeout
    headless: bool = True,
    block_resources: bool = True,
    extra_kwargs: dict[str, Any] | None = None,
    cookies: list[CookieIn] | None = None,
) -> AsyncIterator[Any]:
    """Yield a Scrapling fetcher configured for scraping.

    Lazy-imports ``scrapling`` so consumers without the ``[hostile]``
    extra installed still see the module-level docstring + helpful
    error rather than an opaque ``ModuleNotFoundError`` at import time.

    Parameters
    ----------
    timeout : float
        Per-fetch timeout in seconds. Default 30 s — significantly
        longer than the M1 httpx default because browser warm-up adds
        latency.
    headless : bool
        Run Chromium without a visible window. Default ``True``.
    block_resources : bool
        Skip loading images, fonts, and stylesheets to speed up
        product-page fetches. Default ``True``. Disable when the page
        gates content rendering on resource loads (rare).
    extra_kwargs : dict[str, Any], optional
        Forwarded to every ``StealthyFetcher.async_fetch`` call. Use for
        advanced Scrapling features (extra browser flags, ``user_data_dir``,
        etc.). Avoid in normal flows — defaults are tuned.
    cookies : list[CookieIn], optional
        Caller-supplied cookies, converted to Playwright shape and passed to
        every ``StealthyFetcher.async_fetch`` call. Callers are expected to
        have narrowed these to the
        target URL already.

        Passed inside a guard, unlike every other kwarg here.
        ``async_fetch`` is ``(url, **kwargs)`` typed only by a TypedDict, so
        there is no signature to inspect and no way to know statically whether this version accepts
        ``cookies``. A rejected kwarg raises :class:`CookieKwargUnsupported`
        rather than being swallowed, so the caller can record the tier as
        skipped with a real reason instead of silently returning a logged-out
        page.

    Yields
    ------
    An object with ``await fetcher.async_fetch(url, **kwargs)``; per-call
    kwargs override the defaults above. The response it returns exposes
    ``.html_content`` / ``.status`` / ``.cookies``.

    Raises
    ------
    ImportError
        If the ``[hostile]`` extra is not installed.
    CookieKwargUnsupported
        From ``async_fetch``, if ``cookies`` was supplied and this Scrapling
        version rejects it.
    """
    # Refuse before launching anything: under strict mode this tier cannot
    # vet the requests it will make, and starting a browser first would
    # only make the refusal slower.
    assert_tier_allowed("d")

    try:
        from scrapling.fetchers import (  # noqa: PLC0415
            StealthyFetcher,
        )
    except ImportError as exc:
        raise ImportError(_HOSTILE_NOT_INSTALLED) from exc

    # Every option rides on each fetch. Scrapling takes them on the
    # ``async_fetch`` classmethod; ``StealthyFetcher(...)`` has been a
    # warning-only no-op since 0.3, and until 4.6.0 this helper constructed
    # one, so headless, resource blocking, the timeout and the caller's cookies
    # were all silently dropped. Scrapling's timeout is in milliseconds.
    fetch_defaults: dict[str, Any] = {
        "headless": headless,
        "disable_resources": block_resources,
        "timeout": round(timeout * 1000),
    }
    if extra_kwargs:
        fetch_defaults.update(extra_kwargs)
    if cookies:
        from scrapper_tool.cookies import to_playwright  # noqa: PLC0415

        fetch_defaults["cookies"] = to_playwright(cookies)

    _logger.info(
        "patterns.d.hostile_client.start",
        timeout=timeout,
        headless=headless,
        block_resources=block_resources,
        cookies=len(cookies) if cookies else 0,
    )
    try:
        yield _HostileFetcher(StealthyFetcher, fetch_defaults, carries_cookies=bool(cookies))
    finally:
        # Nothing to close: each Scrapling fetch opens and closes its own
        # browser session.
        _logger.info("patterns.d.hostile_client.closed")


class _HostileFetcher:
    """What :func:`hostile_client` yields: ``async_fetch`` with our defaults applied."""

    def __init__(
        self, fetcher_cls: Any, defaults: dict[str, Any], *, carries_cookies: bool
    ) -> None:
        self._fetcher_cls = fetcher_cls
        self._defaults = defaults
        self._carries_cookies = carries_cookies

    async def async_fetch(self, url: str, **kwargs: Any) -> Any:
        """Fetch ``url``; per-call kwargs override the client's defaults."""
        try:
            return await self._fetcher_cls.async_fetch(url, **{**self._defaults, **kwargs})
        except TypeError as exc:
            # Only a cookie-shaped rejection is translated. Every other TypeError
            # is a real bug in our kwargs and must keep propagating unchanged.
            if self._carries_cookies and "cookie" in str(exc).lower():
                raise CookieKwargUnsupported(str(exc)) from exc
            raise


__all__ = [
    "CookieKwargUnsupported",
    "hostile_client",
]
