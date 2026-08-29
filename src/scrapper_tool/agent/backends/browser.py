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
from typing import TYPE_CHECKING, Any, Literal, Protocol

from scrapper_tool._logging import get_logger
from scrapper_tool.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from scrapper_tool.agent.backends.behavior import BehaviorPolicy
    from scrapper_tool.agent.backends.fingerprint import FingerprintGenerator

_logger = get_logger(__name__)


# --- Public surface -------------------------------------------------------


def _free_port() -> int:
    """Ask the OS for an unused TCP port.

    Racy in principle — the port is released before Chromium binds it — but a
    fixed port breaks the moment two browsers run concurrently, which is the
    normal case for a crawl.
    """
    import socket  # noqa: PLC0415

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class BrowserHandle:
    """Opaque handle returned by :meth:`BrowserBackend.launch`.

    ``playwright_browser`` is the Playwright/Patchright/Camoufox
    ``Browser`` instance — None for backends that don't expose one
    (e.g. Scrapling's fetcher is HTTP-shaped).

    ``raw`` is the backend-native object (e.g. the Scrapling fetcher).
    Callers that target a specific backend can downcast.

    ``shutdown`` is the async cleanup coroutine to ``await`` on close.

    ``cdp_url`` is a Chrome-DevTools-Protocol endpoint for this browser, or None
    when the backend can't offer one. It exists because browser-use 0.13 dropped
    the ability to be handed a live Playwright context and now attaches over CDP
    only — so this is what decides whether E2 can drive our stealth browser or
    would silently launch its own. Camoufox leaves it None on purpose: it's
    Firefox, and Firefox removed CDP in favour of WebDriver BiDi.
    """

    name: str
    playwright_browser: Any | None
    raw: Any
    shutdown: Any  # async callable, no args
    cdp_url: str | None = None

    async def close(self) -> None:
        if self.shutdown is None:
            return
        result = self.shutdown()
        if hasattr(result, "__await__"):
            await result


@dataclass(frozen=True)
class BrowserLaunchOptions:
    """Launch-time knobs handed to a browser backend.

    One object instead of an ever-growing kwargs list on :meth:`launch`.
    Backends read the fields they understand and ignore the rest — the render/
    stealth knobs below are Camoufox-native and are no-ops for Patchright /
    Obscura / Scrapling.

    ``user_data_dir`` is what makes a browser session *persistent*: cookies
    (including Cloudflare's ``cf_clearance``) survive between launches against
    the same directory. Threading it here is what lets the cascade's shared
    profile dir actually reach the browser.

    (``screen`` constraints are not exposed yet — they need a Browserforge
    ``Screen`` object; add when a caller needs it.)
    """

    headful: bool = False
    proxy: str | None = None
    user_data_dir: str | None = None
    headless_mode: Literal["headless", "virtual"] = "headless"
    # WARNING: blocking images is a speed/bandwidth win but Camoufox itself warns
    # it "has been reported to cause detection issues on major WAFs". Use it for
    # unprotected / speed-critical scrapes only — NOT on hard targets.
    block_images: bool = False
    fingerprint_preset: bool = False
    os: str | None = None
    locale: str | None = None


@dataclass(frozen=True)
class BrowserCapabilities:
    """What a backend can *structurally* do, independent of how well it evades.

    This exists because the cascade needs to answer two different questions and
    they have different answers:

    - *Can this tier run on this backend at all?* — a hard yes/no. E2 attaches
      over CDP only, so a backend with ``cdp=False`` cannot host it no matter how
      good its stealth is. Encoding that here is what stops the cascade from ever
      constructing the camoufox+E2 combination that used to raise a 503.
    - *Which backend should we try next?* — a preference, not a ranking. See
      ``BACKEND_FALLBACK_ORDER`` for why this deliberately is not a strength
      ordering.

    ``engine`` matters beyond CDP: retrying a block on a second Chromium is far
    less likely to help than retrying on a different engine, because most
    fingerprinting keys off the engine family.
    """

    name: str
    engine: Literal["firefox", "chromium", "http"]
    cdp: bool
    extra: str


BACKEND_CAPABILITIES: dict[str, BrowserCapabilities] = {
    "camoufox": BrowserCapabilities(
        name="camoufox", engine="firefox", cdp=False, extra="llm-agent"
    ),
    "patchright": BrowserCapabilities(
        name="patchright", engine="chromium", cdp=True, extra="llm-agent"
    ),
    "obscura": BrowserCapabilities(name="obscura", engine="chromium", cdp=True, extra="llm-agent"),
    "scrapling": BrowserCapabilities(name="scrapling", engine="http", cdp=False, extra="hostile"),
}

# Order to *try*, not a ranking of strength — the distinction matters and was
# paid for. On tascaparts.com, Patchright earned a hard "you have been blocked"
# WAF page where Camoufox got a clean 200; on other targets the reverse holds.
# Backends are complementary, so the only defensible ordering is "engine we have
# not tried yet, cheapest first". Camoufox leads because it is the measured best
# single choice; Patchright follows because it changes *engine*, which is the
# variable most likely to change the outcome. Obscura needs an external server,
# so it is last among the browsers.
BACKEND_FALLBACK_ORDER: tuple[str, ...] = ("camoufox", "patchright", "obscura", "scrapling")


def backends_supporting(*, cdp: bool | None = None) -> tuple[str, ...]:
    """Backend names matching a capability filter, in fallback order.

    ``cdp=True`` is what E2 asks for. The filter is the whole mechanism behind
    "impossible combinations cannot be constructed": E2 never sees Camoufox in
    its candidate list, so there is nothing to refuse.
    """
    names = []
    for name in BACKEND_FALLBACK_ORDER:
        caps = BACKEND_CAPABILITIES[name]
        if cdp is not None and caps.cdp != cdp:
            continue
        names.append(name)
    return tuple(names)


class BrowserBackend(Protocol):
    """Protocol implemented by all browser backends."""

    name: str

    async def launch(
        self,
        *,
        options: BrowserLaunchOptions,
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

    async def launch(
        self,
        *,
        options: BrowserLaunchOptions,
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

        # ``headless="virtual"`` runs under an Xvfb virtual display, which is
        # meaningfully stealthier than pure headless (our Docker image ships
        # xvfb). Otherwise fall back to the plain headless/headful boolean.
        headless: bool | str
        headless = "virtual" if options.headless_mode == "virtual" else (not options.headful)

        kwargs: dict[str, Any] = {
            "headless": headless,
            # Camoufox's geo+humanize features improve realism out of the box.
            "humanize": True,
            "geoip": True,
        }
        if options.proxy:
            kwargs["proxy"] = {"server": options.proxy}
        # Persistent profile — cookies (incl. cf_clearance) survive between
        # launches against the same dir. Camoufox honours user_data_dir only
        # with persistent_context=True, so both must be set together.
        if options.user_data_dir:
            kwargs["user_data_dir"] = options.user_data_dir
            kwargs["persistent_context"] = True
        if options.block_images:
            # Camoufox emits a LeakWarning here: blocking images is reported to
            # cause detection issues on major WAFs. Surface it in our own logs so
            # it's visible in structured output, and leave Camoufox's warning
            # intact rather than silencing it with i_know_what_im_doing.
            _logger.warning(
                "agent.browser.camoufox.block_images_stealth_tradeoff",
                detail="block_images speeds up loads but can trip WAF detection; "
                "avoid on protected targets",
            )
            kwargs["block_images"] = True
        if options.fingerprint_preset:
            kwargs["fingerprint_preset"] = True
        if options.os:
            kwargs["os"] = options.os
        if options.locale:
            kwargs["locale"] = options.locale

        # Camoufox is untyped on some installs but typed on others; tolerate both.
        ctx = AsyncCamoufox(**kwargs)  # type: ignore[no-untyped-call,unused-ignore]
        browser = await ctx.__aenter__()

        async def shutdown() -> None:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception as exc:
                _logger.warning("agent.browser.camoufox.close_failed", error=str(exc))

        _logger.info(
            "agent.browser.camoufox.launched",
            headless=headless,
            persistent=bool(options.user_data_dir),
            block_images=options.block_images,
        )
        return BrowserHandle(
            name="camoufox",
            playwright_browser=browser,
            raw=browser,
            shutdown=shutdown,
            # No cdp_url: Camoufox is Firefox, and Firefox dropped CDP in favour
            # of WebDriver BiDi. That makes it undrivable by browser-use 0.13+
            # (CDP-only) — see agent/browse.py, which fails loudly rather than
            # letting E2 fall back to its own browser. Camoufox stays the backend
            # for the render tier and E1, where it measurably wins.
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
        options: BrowserLaunchOptions,
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

        launch_kwargs: dict[str, Any] = {"headless": not options.headful}
        if options.proxy:
            launch_kwargs["proxy"] = {"server": options.proxy}
        # Expose CDP on a free port. Playwright's own ws endpoint is not CDP, and
        # browser-use 0.13 attaches over CDP only — without this, E2 would
        # quietly launch its own unpatched Chromium instead of using this one.
        debug_port = _free_port()
        launch_kwargs["args"] = [f"--remote-debugging-port={debug_port}"]
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

        _logger.info("agent.browser.patchright.launched", headful=options.headful)
        return BrowserHandle(
            name="patchright",
            playwright_browser=browser,
            raw=browser,
            shutdown=shutdown,
            cdp_url=f"http://127.0.0.1:{debug_port}",
        )


# --- Obscura (connect-over-CDP, experimental) -----------------------------


# Playwright's connect_over_cdp discovers the browser ws endpoint from the
# HTTP CDP endpoint, so the http:// form is what works against an Obscura
# server (the bare ws:// form drops during the handshake).
_OBSCURA_DEFAULT_CDP_URL = "http://127.0.0.1:9222"


def resolve_obscura_cdp_url(explicit: str | None = None) -> str:
    """Obscura CDP endpoint: explicit arg -> env -> conventional default.

    Public so E1 (Crawl4AI, extract.py) and E2 (browser-use, browse.py) resolve
    it identically — otherwise each would re-hardcode the default and they'd
    drift the first time one changed.
    """
    return explicit or os.environ.get(
        "SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL", _OBSCURA_DEFAULT_CDP_URL
    )


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
        self._cdp_url = resolve_obscura_cdp_url(cdp_url)

    async def launch(
        self,
        *,
        options: BrowserLaunchOptions,
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
        _ = (options.headful, options.proxy)

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
            # Obscura *is* a CDP server, so E2 can attach to the very browser we
            # just connected to rather than launching a second one.
            cdp_url=self._cdp_url,
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
        options: BrowserLaunchOptions,
        fingerprint: FingerprintGenerator,
        behavior: BehaviorPolicy,
    ) -> BrowserHandle:
        try:
            from scrapper_tool.patterns.d import hostile_client  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(_SCRAPLING_NOT_INSTALLED) from exc

        _ = fingerprint
        _ = behavior

        ctx_mgr = hostile_client(headless=not options.headful)
        fetcher = await ctx_mgr.__aenter__()

        async def shutdown() -> None:
            try:
                await ctx_mgr.__aexit__(None, None, None)
            except Exception as exc:
                _logger.warning("agent.browser.scrapling.close_failed", error=str(exc))

        _ = options.proxy  # Scrapling pulls proxy from ENV / fetcher config

        _logger.info("agent.browser.scrapling.launched", headful=options.headful)
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
        raise ConfigurationError(msg)
    if name == "obscura":
        return ObscuraBackend(cdp_url=cdp_url)
    return table[name]()


async def resolve_context(pw_browser: Any) -> Any:
    """Return a usable Playwright ``BrowserContext`` from whatever a backend handed us.

    Backends return one of two shapes through ``BrowserHandle.playwright_browser``,
    and callers must not care which:

    * a **Browser** — ``chromium.launch()`` (Patchright), ``connect_over_cdp()``
      (Obscura), or Camoufox in its non-persistent mode.
    * a **BrowserContext** — Camoufox with ``user_data_dir`` set, because
      ``persistent_context=True`` makes it call ``launch_persistent_context()``,
      whose return type is a context. Its own ``__aenter__`` is annotated
      ``Union[Browser, BrowserContext]``, so this is contract, not accident.

    The second case is why this helper exists. A ``BrowserContext`` has neither
    ``.contexts`` nor ``.new_context``, so the obvious
    ``browser.contexts[0] if ... else await browser.new_context()`` idiom raises
    ``AttributeError`` on it — and the cascade sets ``user_data_dir`` on every
    ``mode="auto"`` run once ``[hostile]`` is installed, which is the documented
    default install. The render tier therefore failed on its own default path.

    Preferring an existing context over creating one also matters on Obscura:
    ``new_context()`` against a CDP-attached browser opens a fresh *incognito*
    context that the page we then drive would not be in.
    """
    contexts = getattr(pw_browser, "contexts", None)
    if contexts:
        # NOTE: Playwright stores contexts in a `set`, so `contexts[0]` is only
        # deterministic while exactly one exists. Every backend here creates at
        # most one, but don't rely on the index if that ever changes.
        return contexts[0]
    new_context = getattr(pw_browser, "new_context", None)
    if callable(new_context):
        return await new_context()
    # Already a context (Camoufox persistent mode) — use it directly.
    return pw_browser


@asynccontextmanager
async def open_browser(
    backend: BrowserBackend,
    *,
    options: BrowserLaunchOptions,
    fingerprint: FingerprintGenerator,
    behavior: BehaviorPolicy,
) -> AsyncIterator[BrowserHandle]:
    """Async context manager — launches and reliably closes a browser."""
    handle = await backend.launch(options=options, fingerprint=fingerprint, behavior=behavior)
    try:
        yield handle
    finally:
        await handle.close()


__all__ = [
    "BACKEND_CAPABILITIES",
    "BACKEND_FALLBACK_ORDER",
    "BrowserBackend",
    "BrowserCapabilities",
    "BrowserHandle",
    "BrowserLaunchOptions",
    "CamoufoxBackend",
    "ObscuraBackend",
    "PatchrightBackend",
    "ScraplingBackend",
    "backends_supporting",
    "get_browser_backend",
    "open_browser",
    "resolve_context",
]
