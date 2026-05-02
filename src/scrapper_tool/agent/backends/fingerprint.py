"""Per-session fingerprint generation for non-Camoufox browser backends.

Camoufox patches the Firefox engine at the C++ level and brings its own
fingerprint surface. Patchright / Zendriver / Botasaurus drive a
stock-ish Chromium and need an injected fingerprint (UA, Accept-*,
Sec-CH-UA, viewport, screen, fonts, Canvas/WebGL/AudioContext noise) to
look like a real browser.

We delegate the hard work to Browserforge (`pip install browserforge`)
which curates a database of real-browser headers and inject scripts.
The ``BrowserforgeGenerator`` is a thin lazy-import wrapper.
"""

from __future__ import annotations

from typing import Protocol

from scrapper_tool._logging import get_logger

_logger = get_logger(__name__)


class GeneratedFingerprint:
    """Bundled output of a fingerprint generator.

    Backend-agnostic — backends pick the bits they understand:
    Patchright applies ``user_agent`` / ``viewport`` / ``locale`` to a
    Playwright context, Zendriver applies them via CDP overrides, etc.
    """

    __slots__ = ("headers", "init_scripts", "locale", "user_agent", "viewport")

    def __init__(
        self,
        *,
        user_agent: str,
        viewport: tuple[int, int],
        locale: str,
        headers: dict[str, str],
        init_scripts: list[str],
    ) -> None:
        self.user_agent = user_agent
        self.viewport = viewport
        self.locale = locale
        self.headers = headers
        self.init_scripts = init_scripts


class FingerprintGenerator(Protocol):
    """Protocol implemented by per-session fingerprint generators."""

    name: str

    def generate(self) -> GeneratedFingerprint: ...


class NoOpGenerator:
    """Returns an empty/identity fingerprint.

    Used by Camoufox (which has its own) and as the default when
    ``fingerprint="none"``. Keeps the backend code path uniform.
    """

    name = "none"

    def generate(self) -> GeneratedFingerprint:
        return GeneratedFingerprint(
            user_agent="",
            viewport=(1280, 800),
            locale="en-US",
            headers={},
            init_scripts=[],
        )


_BROWSERFORGE_NOT_INSTALLED = (
    "Browserforge fingerprint generator requires the [llm-agent] extra.\n"
    "Install with: pip install scrapper-tool[llm-agent]\n"
    "Or set fingerprint='none' to disable per-session fingerprint randomization."
)


class BrowserforgeGenerator:
    """Per-session randomized fingerprint via Browserforge.

    Lazy-imports ``browserforge`` so the package still imports without
    the ``[llm-agent]`` extra. Each ``generate()`` call returns a fresh
    realistic fingerprint — call once per browser-context launch.
    """

    name = "browserforge"

    def __init__(self, *, browser: str = "chrome", os_family: str = "windows") -> None:
        self._browser = browser
        self._os = os_family

    def generate(self) -> GeneratedFingerprint:
        try:
            from browserforge.fingerprints import (  # noqa: PLC0415
                FingerprintGenerator as _BFGenerator,
            )
            from browserforge.headers import HeaderGenerator  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — covered by unit mock
            raise ImportError(_BROWSERFORGE_NOT_INSTALLED) from exc

        # Browserforge's generator surfaces vary across releases — be
        # defensive and fall back to header-only if the fingerprint
        # generator API moved. The header generator is the more stable
        # surface and gives us UA + Accept-* which is what backends
        # actually consume.
        headers: dict[str, str] = {}
        try:
            hg = HeaderGenerator(browser=self._browser, os=self._os)
            headers = dict(hg.generate())
        except Exception as exc:
            _logger.warning("agent.fingerprint.browserforge_header_gen_failed", error=str(exc))

        user_agent = headers.get("User-Agent") or headers.get("user-agent") or ""

        # Try to extract richer fingerprint (canvas, WebGL, …) — best-effort.
        init_scripts: list[str] = []
        viewport = (1280, 800)
        locale = "en-US"
        try:
            bfg = _BFGenerator(browser=self._browser, os=self._os)
            fp = bfg.generate()
            # API drift across versions: try common attrs, fall back gracefully.
            screen = getattr(fp, "screen", None)
            if screen is not None and hasattr(screen, "width") and hasattr(screen, "height"):
                viewport = (int(screen.width), int(screen.height))
            navigator = getattr(fp, "navigator", None)
            if navigator is not None and hasattr(navigator, "language"):
                locale = str(navigator.language)
        except Exception as exc:
            _logger.debug("agent.fingerprint.browserforge_fp_gen_skipped", error=str(exc))

        return GeneratedFingerprint(
            user_agent=user_agent,
            viewport=viewport,
            locale=locale,
            headers=headers,
            init_scripts=init_scripts,
        )


def get_fingerprint_generator(name: str) -> FingerprintGenerator:
    """Resolve a fingerprint generator by name.

    Unknown names raise :class:`ValueError` rather than silently
    returning a no-op — typos in env vars should be loud.
    """
    if name == "browserforge":
        return BrowserforgeGenerator()
    if name == "none":
        return NoOpGenerator()
    msg = f"Unknown fingerprint generator: {name!r}. Choices: 'browserforge', 'none'."
    raise ValueError(msg)


__all__ = [
    "BrowserforgeGenerator",
    "FingerprintGenerator",
    "GeneratedFingerprint",
    "NoOpGenerator",
    "get_fingerprint_generator",
]
