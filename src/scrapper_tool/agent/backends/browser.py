"""Stealth browser backends for Pattern E.

Default = :class:`CamoufoxBackend` — Firefox fork, ~0% headless detection
on 2026 benchmarks (CreepJS / DataDome / CF Turnstile / Imperva /
reCAPTCHA / Fingerprint.com / most WAFs). Heaviest, ~200 MB/instance.

Alternatives:

- :class:`PatchrightBackend` — Patchright (Python drop-in for Playwright,
  C++ Chromium patches, ~67% detection reduction, 5-10x faster than
  Camoufox). "Fast mode" for unprotected/lightly-protected sites.
- :class:`ScraplingBackend` — reuses the existing ``[hostile]`` extra.

Both expose a Playwright ``Browser`` so browser-use / Crawl4AI can drive
them. A lightweight CDP-direct option (Obscura) can be added as a
connect-over-CDP backend without breaking this contract.

All backends lazy-import their dependencies. The package still imports
without ``[llm-agent]`` installed; ``launch()`` raises a helpful
:class:`ImportError` if the relevant extra is missing.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from scrapper_tool._logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from scrapper_tool.agent.backends.behavior import BehaviorPolicy
    from scrapper_tool.agent.backends.fingerprint import FingerprintGenerator

_logger = get_logger(__name__)


# --- Public surface -------------------------------------------------------


@dataclass
class BrowserHandle:
    """Opaque handle returned by :meth:`BrowserBackend.launch`.

    ``playwright_browser`` is the Playwright/Patchright/Camoufox
    ``Browser`` instance — None for backends that don't expose one
    (e.g. Scrapling's fetcher is HTTP-shaped).

    ``raw`` is the backend-native object (e.g. the Scrapling fetcher).
    Callers that target a specific backend can downcast.

    ``shutdown`` is the async cleanup coroutine to ``await`` on close.
    """

    name: str
    playwright_browser: Any | None
    raw: Any
    shutdown: Any  # async callable, no args

    async def close(self) -> None:
        if self.shutdown is None:
            return
        result = self.shutdown()
        if hasattr(result, "__await__"):
            await result


class BrowserBackend(Protocol):
    """Protocol implemented by all browser backends."""

    name: str

    async def launch(
        self,
        *,
        headful: bool,
        proxy: str | None,
        fingerprint: FingerprintGenerator,
        behavior: BehaviorPolicy,
    ) -> BrowserHandle:
        """Start a browser instance and return a :class:`BrowserHandle`.

        Caller is responsible for calling ``handle.close()``.
        """


# --- Camoufox (default) ---------------------------------------------------


_CAMOUFOX_NOT_INSTALLED = (
    "Camoufox browser backend requires the [llm-agent] extra.\n"
    "Install with: pip install scrapper-tool[llm-agent]\n"
    "Then run: camoufox fetch  (downloads the patched Firefox once, ~300 MB).\n"
    "Or set browser='patchright' / 'scrapling' for a different backend."
)


class CamoufoxBackend:
    """Camoufox — Firefox fork with C++-level stealth patches.

    Highest bypass rate of any open-source backend in 2026 benchmarks
    (~0% headless detection across major detectors). Cost: ~200 MB
    RAM/instance and a one-time ~300 MB Firefox download. Use this
    unless install size or per-page latency dominates.
    """

    name = "camoufox"

    async def launch(  # pragma: no cover — requires real Camoufox install
        self,
        *,
        headful: bool,
        proxy: str | None,
        fingerprint: FingerprintGenerator,
        behavior: BehaviorPolicy,
    ) -> BrowserHandle:
        try:
            from camoufox.async_api import AsyncCamoufox  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(_CAMOUFOX_NOT_INSTALLED) from exc

        # Camoufox brings its own fingerprint surface — we ignore the
        # injected fingerprint generator (caller should set
        # ``fingerprint='none'`` for clarity).
        _ = fingerprint  # explicitly drop — Camoufox-internal
        _ = behavior  # behavior shaping is applied by callers per-action

        kwargs: dict[str, Any] = {
            "headless": not headful,
            # Camoufox's geo+humanize features improve realism out of the box.
            "humanize": True,
            "geoip": True,
        }
        if proxy:
            kwargs["proxy"] = {"server": proxy}

        # Camoufox is untyped on some installs but typed on others; tolerate both.
        ctx = AsyncCamoufox(**kwargs)  # type: ignore[no-untyped-call,unused-ignore]
        browser = await ctx.__aenter__()

        async def shutdown() -> None:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception as exc:
                _logger.warning("agent.browser.camoufox.close_failed", error=str(exc))

        _logger.info("agent.browser.camoufox.launched", headful=headful)
        return BrowserHandle(
            name="camoufox",
            playwright_browser=browser,
            raw=browser,
            shutdown=shutdown,
        )


# --- Patchright (fast mode) -----------------------------------------------


_PATCHRIGHT_NOT_INSTALLED = (
    "Patchright browser backend requires the [llm-agent] extra.\n"
    "Install with: pip install scrapper-tool[llm-agent]\n"
    "Then run: patchright install chromium  (downloads patched Chromium ~250 MB)."
)


class PatchrightBackend:
    """Patchright — Playwright fork with C++ Chromium stealth patches.

    "Fast mode" alternative to Camoufox. ~5-10x faster per page and
    half the RAM (~120 MB), but only ~67% headless detection reduction —
    fails on harder Cloudflare Enterprise / DataDome variants. Use for
    unprotected/lightly-protected sites or when batch throughput matters
    more than a 100% bypass rate.
    """

    name = "patchright"

    async def launch(  # pragma: no cover — requires real Patchright install
        self,
        *,
        headful: bool,
        proxy: str | None,
        fingerprint: FingerprintGenerator,
        behavior: BehaviorPolicy,
    ) -> BrowserHandle:
        try:
            from patchright.async_api import async_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(_PATCHRIGHT_NOT_INSTALLED) from exc

        _ = behavior  # applied per-action by callers

        pw_ctx = async_playwright()
        pw = await pw_ctx.__aenter__()

        fp = fingerprint.generate()

        launch_kwargs: dict[str, Any] = {"headless": not headful}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        # Patchright recommends Chromium for the stealth patches.
        browser = await pw.chromium.launch(**launch_kwargs)

        # Apply fingerprint via a default context — the agent loop can
        # create per-task contexts if it needs isolation.
        context_kwargs: dict[str, Any] = {}
        if fp.user_agent:
            context_kwargs["user_agent"] = fp.user_agent
        if fp.viewport:
            context_kwargs["viewport"] = {"width": fp.viewport[0], "height": fp.viewport[1]}
        if fp.locale:
            context_kwargs["locale"] = fp.locale
        if fp.headers:
            context_kwargs["extra_http_headers"] = fp.headers

        if context_kwargs:
            await browser.new_context(**context_kwargs)

        async def shutdown() -> None:
            try:
                await browser.close()
                await pw_ctx.__aexit__(None, None, None)
            except Exception as exc:
                _logger.warning("agent.browser.patchright.close_failed", error=str(exc))

        _logger.info("agent.browser.patchright.launched", headful=headful)
        return BrowserHandle(
            name="patchright",
            playwright_browser=browser,
            raw=browser,
            shutdown=shutdown,
        )


# --- Obscura (connect-over-CDP, experimental) -----------------------------


# Playwright's connect_over_cdp discovers the browser ws endpoint from the
# HTTP CDP endpoint, so the http:// form is what works against an Obscura
# server (the bare ws:// form drops during the handshake).
_OBSCURA_DEFAULT_CDP_URL = "http://127.0.0.1:9222"

_OBSCURA_NOT_INSTALLED = (
    "Obscura backend connects to a running Obscura CDP server via Playwright.\n"
    "Install the [llm-agent] extra (brings Playwright): pip install scrapper-tool[llm-agent]\n"
    "Then run the server, e.g.: docker run -p 9222:9222 h4ckf0r0day/obscura serve --stealth\n"
    "and point SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL at it (default http://127.0.0.1:9222)."
)


class ObscuraBackend:
    """Obscura — Rust CDP-server headless browser, driven over ``connect_over_cdp``.

    **Experimental / benchmark-gated.** Obscura (``obscura serve``) exposes
    a Chrome DevTools Protocol WebSocket; Playwright ``connect_over_cdp``
    yields a real ``Browser``, so — unlike the removed Zendriver/Botasaurus —
    it drives browser-use / Crawl4AI directly. It is lightweight (~30 MB
    RAM, ~85 ms loads) but its stealth is unproven next to Camoufox. Keep
    Camoufox the default; measure Obscura's real detection rate via the
    ``canary`` CLI before trusting it on protected targets.

    The server is external (run as a sidecar) — ``shutdown`` closes the
    Playwright connection but does NOT stop the Obscura process.
    """

    name = "obscura"

    def __init__(self, *, cdp_url: str | None = None) -> None:
        # Resolution order: explicit arg -> env -> conventional default.
        self._cdp_url = cdp_url or os.environ.get(
            "SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL", _OBSCURA_DEFAULT_CDP_URL
        )

    async def launch(
        self,
        *,
        headful: bool,
        proxy: str | None,
        fingerprint: FingerprintGenerator,
        behavior: BehaviorPolicy,
    ) -> BrowserHandle:
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(_OBSCURA_NOT_INSTALLED) from exc

        _ = behavior  # applied per-action via the page-hook layer
        # ``headful`` / ``proxy`` are properties of the external Obscura
        # server (``obscura serve --proxy ...``), not the CDP client, so we
        # can only connect to whatever the server was started with.
        _ = (headful, proxy)

        pw_ctx = async_playwright()
        pw = await pw_ctx.__aenter__()
        try:
            browser = await pw.chromium.connect_over_cdp(self._cdp_url)
        except Exception as exc:
            await pw_ctx.__aexit__(None, None, None)
            msg = f"Obscura CDP connect failed at {self._cdp_url!r}: {exc}"
            raise ImportError(msg) from exc

        # Apply the injected fingerprint to a default context, mirroring
        # Patchright (Obscura is Chromium-class over CDP).
        fp = fingerprint.generate()
        context_kwargs: dict[str, Any] = {}
        if fp.user_agent:
            context_kwargs["user_agent"] = fp.user_agent
        if fp.viewport:
            context_kwargs["viewport"] = {"width": fp.viewport[0], "height": fp.viewport[1]}
        if fp.locale:
            context_kwargs["locale"] = fp.locale
        if fp.headers:
            context_kwargs["extra_http_headers"] = fp.headers
        if context_kwargs:
            await browser.new_context(**context_kwargs)

        async def shutdown() -> None:
            try:
                await browser.close()
            except Exception as exc:
                _logger.warning("agent.browser.obscura.close_failed", error=str(exc))
            try:
                await pw_ctx.__aexit__(None, None, None)
            except Exception as exc:  # pragma: no cover — defensive
                _logger.warning("agent.browser.obscura.pw_stop_failed", error=str(exc))

        _logger.info("agent.browser.obscura.launched", cdp_url=self._cdp_url)
        return BrowserHandle(
            name="obscura",
            playwright_browser=browser,
            raw=browser,
            shutdown=shutdown,
        )


# --- Scrapling (reuses [hostile] extra) -----------------------------------


_SCRAPLING_NOT_INSTALLED = (
    "Scrapling browser backend requires the [hostile] extra.\n"
    "Install with: pip install scrapper-tool[hostile]"
)


class ScraplingBackend:
    """Reuse the existing Pattern D ``hostile_client`` Scrapling fetcher.

    Convenient when ``[hostile]`` is already installed and the user wants
    Pattern E without pulling another browser. Limitation: Scrapling's
    fetcher API is HTTP-shaped (one URL per call) — driving it through
    a multi-step browse loop is awkward, so prefer Camoufox/Patchright
    for E2.
    """

    name = "scrapling"

    async def launch(  # pragma: no cover — requires real Scrapling install
        self,
        *,
        headful: bool,
        proxy: str | None,
        fingerprint: FingerprintGenerator,
        behavior: BehaviorPolicy,
    ) -> BrowserHandle:
        try:
            from scrapper_tool.patterns.d import hostile_client  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(_SCRAPLING_NOT_INSTALLED) from exc

        _ = fingerprint
        _ = behavior

        ctx_mgr = hostile_client(headless=not headful)
        fetcher = await ctx_mgr.__aenter__()

        async def shutdown() -> None:
            try:
                await ctx_mgr.__aexit__(None, None, None)
            except Exception as exc:
                _logger.warning("agent.browser.scrapling.close_failed", error=str(exc))

        _ = proxy  # Scrapling pulls proxy from ENV / fetcher config

        _logger.info("agent.browser.scrapling.launched", headful=headful)
        return BrowserHandle(
            name="scrapling",
            playwright_browser=None,
            raw=fetcher,
            shutdown=shutdown,
        )


# --- Resolver -------------------------------------------------------------


def get_browser_backend(name: str, *, cdp_url: str | None = None) -> BrowserBackend:
    """Build a browser backend by name.

    ``cdp_url`` is forwarded to backends that connect to an external server
    (currently only Obscura). It's ignored by the in-process backends, which
    have no URL to connect to.
    """
    table: dict[str, type[BrowserBackend]] = {
        "camoufox": CamoufoxBackend,
        "patchright": PatchrightBackend,
        "scrapling": ScraplingBackend,
        "obscura": ObscuraBackend,
    }
    if name not in table:
        msg = f"Unknown browser backend: {name!r}. Choices: {sorted(table)}."
        raise ValueError(msg)
    if name == "obscura":
        return ObscuraBackend(cdp_url=cdp_url)
    return table[name]()


@asynccontextmanager
async def open_browser(
    backend: BrowserBackend,
    *,
    headful: bool,
    proxy: str | None,
    fingerprint: FingerprintGenerator,
    behavior: BehaviorPolicy,
) -> AsyncIterator[BrowserHandle]:
    """Async context manager — launches and reliably closes a browser."""
    handle = await backend.launch(
        headful=headful, proxy=proxy, fingerprint=fingerprint, behavior=behavior
    )
    try:
        yield handle
    finally:
        await handle.close()


__all__ = [
    "BrowserBackend",
    "BrowserHandle",
    "CamoufoxBackend",
    "ObscuraBackend",
    "PatchrightBackend",
    "ScraplingBackend",
    "get_browser_backend",
    "open_browser",
]
