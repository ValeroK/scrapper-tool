"""Stealth browser backends for Pattern E.

Default = :class:`CamoufoxBackend` — Firefox fork, ~0% headless detection
on 2026 benchmarks (CreepJS / DataDome / CF Turnstile / Imperva /
reCAPTCHA / Fingerprint.com / most WAFs). Heaviest, ~200 MB/instance.

Alternatives:

- :class:`PatchrightBackend` — Patchright (Python drop-in for Playwright,
  C++ Chromium patches, ~67% detection reduction, 5-10x faster than
  Camoufox). "Fast mode" for unprotected/lightly-protected sites.
- :class:`ZendriverBackend` — CDP-direct fork of nodriver (75% bypass on
  CF/DataDome/Akamai/CloudFront vs nodriver's 25%). Lightest. Useful
  when Playwright API itself is the giveaway.
- :class:`BotasaurusBackend` — decorator-paradigm scraper with
  humanlike-behavior emulation. Wraps a different driver model entirely.
- :class:`ScraplingBackend` — reuses the existing ``[hostile]`` extra.

All backends lazy-import their dependencies. The package still imports
without ``[llm-agent]`` installed; ``launch()`` raises a helpful
:class:`ImportError` if the relevant extra is missing.
"""

from __future__ import annotations

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
    (Zendriver uses raw CDP).

    ``raw`` is the backend-native object (Scrapling fetcher, Zendriver
    Browser, Botasaurus driver). Callers that target a specific backend
    can downcast.

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


# --- Zendriver (CDP-direct) ----------------------------------------------


_ZENDRIVER_NOT_INSTALLED = (
    "Zendriver browser backend requires the [zendriver-backend] extra.\n"
    "Install with: pip install scrapper-tool[llm-agent,zendriver-backend]"
)


class ZendriverBackend:
    """Zendriver — CDP-direct fork of nodriver.

    Bypasses Playwright entirely; drives Chrome via raw CDP. Lightest
    backend (~80 MB RAM) and the highest reported bypass rate among
    Chromium-class drivers (~75% on CF + DataDome + Akamai +
    CloudFront). Trade-off: not a Playwright API, so Crawl4AI and
    browser-use can't drive it directly — Pattern E uses Zendriver
    only via its own session loop in browse mode.
    """

    name = "zendriver"

    async def launch(  # pragma: no cover — requires real Zendriver install
        self,
        *,
        headful: bool,
        proxy: str | None,
        fingerprint: FingerprintGenerator,
        behavior: BehaviorPolicy,
    ) -> BrowserHandle:
        try:
            import zendriver  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(_ZENDRIVER_NOT_INSTALLED) from exc

        _ = behavior  # per-action

        config_kwargs: dict[str, Any] = {"headless": not headful}
        if proxy:
            config_kwargs["browser_args"] = [f"--proxy-server={proxy}"]
        fp = fingerprint.generate()
        if fp.user_agent:
            existing = config_kwargs.get("browser_args", [])
            config_kwargs["browser_args"] = [
                *existing,
                f"--user-agent={fp.user_agent}",
            ]

        # Zendriver's API is `start(**kwargs)` returning a Browser.
        browser = await zendriver.start(**config_kwargs)

        async def shutdown() -> None:
            close = getattr(browser, "stop", None) or getattr(browser, "close", None)
            if close is None:
                return
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                _logger.warning("agent.browser.zendriver.close_failed", error=str(exc))

        _logger.info("agent.browser.zendriver.launched", headful=headful)
        return BrowserHandle(
            name="zendriver",
            playwright_browser=None,
            raw=browser,
            shutdown=shutdown,
        )


# --- Botasaurus -----------------------------------------------------------


_BOTASAURUS_NOT_INSTALLED = (
    "Botasaurus browser backend requires the [botasaurus-backend] extra.\n"
    "Install with: pip install scrapper-tool[llm-agent,botasaurus-backend]"
)


class BotasaurusBackend:
    """Botasaurus — humanlike-behavior-first decorator framework.

    Different paradigm from the other backends — wraps user functions in
    decorators that handle anti-bot mitigations. Pattern E uses its
    underlying ``Driver`` object so the agent loop can drive it directly.
    """

    name = "botasaurus"

    async def launch(  # pragma: no cover — requires real Botasaurus install
        self,
        *,
        headful: bool,
        proxy: str | None,
        fingerprint: FingerprintGenerator,
        behavior: BehaviorPolicy,
    ) -> BrowserHandle:
        try:
            from botasaurus_driver import Driver  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(_BOTASAURUS_NOT_INSTALLED) from exc

        _ = behavior
        _ = fingerprint

        driver_kwargs: dict[str, Any] = {"headless": not headful}
        if proxy:
            driver_kwargs["proxy"] = proxy
        driver = Driver(**driver_kwargs)

        async def shutdown() -> None:
            try:
                close = getattr(driver, "close", None) or getattr(driver, "quit", None)
                if close is not None:
                    close()
            except Exception as exc:
                _logger.warning("agent.browser.botasaurus.close_failed", error=str(exc))

        _logger.info("agent.browser.botasaurus.launched", headful=headful)
        return BrowserHandle(
            name="botasaurus",
            playwright_browser=None,
            raw=driver,
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


def get_browser_backend(name: str) -> BrowserBackend:
    """Build a browser backend by name."""
    table: dict[str, type[BrowserBackend]] = {
        "camoufox": CamoufoxBackend,
        "patchright": PatchrightBackend,
        "zendriver": ZendriverBackend,
        "botasaurus": BotasaurusBackend,
        "scrapling": ScraplingBackend,
    }
    if name not in table:
        msg = f"Unknown browser backend: {name!r}. Choices: {sorted(table)}."
        raise ValueError(msg)
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
    "BotasaurusBackend",
    "BrowserBackend",
    "BrowserHandle",
    "CamoufoxBackend",
    "PatchrightBackend",
    "ScraplingBackend",
    "ZendriverBackend",
    "get_browser_backend",
    "open_browser",
]
