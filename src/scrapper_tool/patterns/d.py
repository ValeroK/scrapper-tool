"""Pattern D — Hostile sites (Cloudflare Turnstile, Akamai EVA, Distil, …).

Last-resort path when the M2 anti-bot ladder
(``chrome133a → chrome124 → safari18_0 → firefox135``) has been
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

1. M2's ladder genuinely returns 403 on **every** profile (chrome133a /
   chrome124 / safari18_0 / firefox135). Capture the 403/200 matrix in
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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_logger = get_logger(__name__)


_HOSTILE_NOT_INSTALLED = (
    "Pattern D requires Scrapling. Install via:\n"
    "    pip install scrapper-tool[hostile]\n"
    "(adds Playwright + Chromium ~400 MB; opt-in for hostile vendors only)."
)


@asynccontextmanager
async def hostile_client(
    *,
    timeout: float = 30.0,  # noqa: ASYNC109 — passed to Scrapling, not asyncio.timeout
    headless: bool = True,
    block_resources: bool = True,
    extra_kwargs: dict[str, Any] | None = None,
) -> AsyncIterator[Any]:
    """Yield a Scrapling ``StealthyFetcher`` configured for scraping.

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
        Forwarded to ``StealthyFetcher.__init__``. Use for advanced
        Scrapling features (custom Playwright launch args, profile
        directory, etc.). Avoid in normal flows — defaults are tuned.

    Yields
    ------
    The Scrapling fetcher instance. Call ``await fetcher.async_fetch(url)``
    to perform a request; the returned response object exposes
    ``.html_content`` / ``.status`` / ``.cookies``.

    Raises
    ------
    ImportError
        If the ``[hostile]`` extra is not installed.
    """
    try:
        from scrapling.fetchers import (  # noqa: PLC0415
            StealthyFetcher,
        )
    except ImportError as exc:
        raise ImportError(_HOSTILE_NOT_INSTALLED) from exc

    init_kwargs: dict[str, Any] = {
        "headless": headless,
        "block_resources": block_resources,
    }
    if extra_kwargs:
        init_kwargs.update(extra_kwargs)

    _logger.info(
        "patterns.d.hostile_client.start",
        timeout=timeout,
        headless=headless,
        block_resources=block_resources,
    )
    # Scrapling is untyped on some installs but typed on others; tolerate both.
    fetcher = StealthyFetcher(**init_kwargs)  # type: ignore[no-untyped-call,unused-ignore]
    try:
        yield fetcher
    finally:
        # Scrapling's StealthyFetcher manages Playwright lifecycle
        # internally; the close-on-exit is best-effort. Newer versions
        # expose ``aclose()``; older expose ``close()``.
        close = getattr(fetcher, "aclose", None) or getattr(fetcher, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
        _logger.info("patterns.d.hostile_client.closed")


__all__ = [
    "hostile_client",
]
