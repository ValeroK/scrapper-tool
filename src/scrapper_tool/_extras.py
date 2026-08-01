"""Stdlib-only capability probes shared by the HTTP sidecar, MCP server, and ``doctor``.

Every probe here answers one question: *is this optional capability actually
usable right now?* They are deliberately cheap — module imports, ``Path.glob``
and one short TCP connect — so callers can run the whole set on a request path
or from a CLI without launching a browser.

Import discipline
-----------------
**This module imports nothing heavier than the standard library at module
level.** Every optional dependency (``scrapling``, ``camoufox``, ``crawl4ai``,
``fastapi``, ...) is imported *inside* the function that needs it. That is what
lets ``doctor`` — which must run on a bare ``pip install scrapper-tool`` — share
the same probes as the ``[http]``-gated sidecar. The dependency graph is
strictly one-directional::

    http_server | mcp | doctor  →  _extras  →  stdlib

``tests/unit/test_extras.py`` enforces this with a subprocess import that
poisons ``sys.modules`` for the heavy packages.

Failure philosophy
------------------
Every probe returns a value; none raise. A probe that cannot answer reports the
pessimistic result (``False`` / ``"unknown"`` / ``None``) so callers surface
*degraded* rather than crashing. A capability check is a diagnostic, and a
diagnostic that can take down the thing it is diagnosing is worse than useless.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = [
    "INSTALL_HINTS",
    "agent_available",
    "agent_runnable",
    "browser_binary_present",
    "browser_use_accepts",
    "check_browser_module",
    "cookie_backend_available",
    "crawl4ai_accepts",
    "crawl4ai_available",
    "geoip2_available",
    "hostile_available",
    "obscura_endpoint_reachable",
    "playwright_browsers_root",
    "probe_llm",
    "render_tier_enabled",
    "user_data_dir_supported",
]

# ---------------------------------------------------------------------------
# Install hints
# ---------------------------------------------------------------------------

#: Remediation commands keyed by capability. ``doctor`` builds its ``Fixes:``
#: block from these, de-duplicated and in insertion order, so the same string
#: is never printed twice for two different failing tiers. Keeping them in one
#: dict is the point: the same ``pip install`` lines were previously duplicated
#: across ``browser.py``, ``d.py`` and ``http_server.py`` and had already
#: drifted in quoting style.
INSTALL_HINTS: dict[str, str] = {
    "hostile": "pip install 'scrapper-tool[hostile]'",
    "llm-agent": "pip install 'scrapper-tool[llm-agent]'",
    "agent": "pip install 'scrapper-tool[agent]'",
    "http": "pip install 'scrapper-tool[http]'",
    "cookies": "pip install 'scrapper-tool[cookies]'",
    "camoufox-binary": "camoufox fetch",
    "playwright-firefox": "playwright install firefox",
    "patchright-chromium": "patchright install chromium",
    "geoip2": "pip install geoip2",
    "ollama": "ollama serve && ollama pull <model>",
}


# ---------------------------------------------------------------------------
# Python-package probes
# ---------------------------------------------------------------------------


def agent_available() -> bool:
    """Return True if the ``[llm-agent]`` extra is installed.

    This is a *Python-package* check only — the ``camoufox`` / ``patchright`` /
    ``crawl4ai`` modules import cleanly. It does NOT guarantee the on-disk
    browser binary is present. For runtime capability use :func:`agent_runnable`.
    """
    try:
        import scrapper_tool.agent  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def hostile_available() -> bool:
    """Return True if the ``[hostile]`` extra (Scrapling) is installed."""
    try:
        import scrapling  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def crawl4ai_available() -> bool:
    """Return True if Crawl4AI (Pattern E1's engine) is importable."""
    try:
        import crawl4ai  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def geoip2_available() -> bool:
    """Return True if ``geoip2`` is importable.

    Camoufox 0.4+ raises a 500 at launch when ``geoip2`` is absent — see the
    dependency comment in ``pyproject.toml``. Surfacing it as its own check
    turns an opaque runtime failure into a one-line fix.
    """
    try:
        import geoip2  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def cookie_backend_available() -> bool:
    """Return True if a browser-cookie backend is importable.

    ``rookiepy`` (MIT) is the declared ``[cookies]`` extra. ``browser_cookie3``
    is **LGPL** and is deliberately never declared as a dependency — but if a
    user already has it in their environment we will happily use it, so it is
    probed here via :func:`importlib.util.find_spec` rather than imported.
    """
    from importlib.util import find_spec  # noqa: PLC0415

    for mod in ("rookiepy", "browser_cookie3"):
        try:
            if find_spec(mod) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


# ---------------------------------------------------------------------------
# On-disk binary probes
# ---------------------------------------------------------------------------


def playwright_browsers_root() -> Path:
    """Return ``$PLAYWRIGHT_BROWSERS_PATH`` (or its default).

    Playwright stores binaries here as ``<browser>-<rev>/...``. Both
    ``playwright install firefox`` and ``patchright install chromium`` write
    into this directory. ``$PLAYWRIGHT_BROWSERS_PATH`` overrides the default;
    we honour it.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "ms-playwright"


def browser_binary_present(browser: str, *, root: Path | None = None) -> bool:  # noqa: PLR0911
    """Probe the on-disk binary for ``browser``.

    True when a launchable binary is found; False when the Python module is
    installed but the binary isn't — the case that bit the published 1.1.0
    image, where ``agent_installed`` was true and ``patchright install
    chromium`` had run, but Firefox was never downloaded, so every Pattern
    E1/E2 attempt failed at runtime.

    ``root`` overrides the Playwright browsers directory; it exists so callers
    that expose their own monkeypatchable ``playwright_browsers_root`` hook
    (``http_server``) keep working, and so tests can point at ``tmp_path``.

    Returns False rather than raising.
    """
    search_root = root if root is not None else playwright_browsers_root()

    if browser == "patchright":
        # Patchright ships a patched Chromium under chromium-<rev>/. The
        # subdirectory is ``chrome-linux64/`` on Linux x64 (default Playwright
        # layout); older images used ``chrome-linux/``. Try both so the probe
        # works against any reasonable Playwright version, and against
        # Patchright's headless-shell variant.
        candidates = (
            "chromium-*/chrome-linux64/chrome",
            "chromium-*/chrome-linux/chrome",
            "chromium_headless_shell-*/chrome-linux64/headless_shell",
            "chromium_headless_shell-*/chrome-linux/headless_shell",
        )
        return any(p.is_file() for pat in candidates for p in search_root.glob(pat))

    if browser == "camoufox":
        # Camoufox stores its Firefox fork under its own path; the python
        # wrapper exposes ``camoufox.path``. browser-use (E2) also pulls
        # Playwright Firefox, so we treat either as runnable.
        try:
            import camoufox  # noqa: PLC0415

            cf_path = getattr(camoufox, "path", None)
            if cf_path and Path(cf_path).is_file():
                return True
        except ImportError:
            pass
        return any(p.is_file() for p in search_root.glob("firefox-*/firefox/firefox"))

    if browser == "scrapling":
        # Scrapling ships its own Camoufox; if either binary is present we call
        # it runnable.
        if any(p.is_file() for p in search_root.glob("firefox-*/firefox/firefox")):
            return True
        try:
            import scrapling  # noqa: F401, PLC0415
        except ImportError:
            return False
        return False

    if browser == "obscura":
        # Obscura is an external CDP server (sidecar), not a local binary.
        return obscura_endpoint_reachable()

    # Unknown browser — be conservative and report False so callers surface the
    # configuration mistake rather than silently passing.
    return False


def obscura_endpoint_reachable(timeout_s: float = 0.5) -> bool:
    """Best-effort TCP reachability probe for the Obscura CDP endpoint.

    Reads ``SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL`` (default
    ``http://127.0.0.1:9222``) and attempts a short blocking connect. Returns
    False on any failure so callers report *degraded* rather than crashing.
    """
    import socket  # noqa: PLC0415
    from urllib.parse import urlparse  # noqa: PLC0415

    url = os.environ.get("SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL", "http://127.0.0.1:9222")
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9222
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def agent_runnable(browser: str, *, root: Path | None = None) -> bool:
    """True when both the Python extra AND the binary are present.

    ``agent_available`` ∧ ``browser_binary_present``. This is the field callers
    should gate Pattern E1/E2 on; ``agent_available`` alone is necessary but not
    sufficient.
    """
    return agent_available() and browser_binary_present(browser, root=root)


# ---------------------------------------------------------------------------
# Library-capability probes
# ---------------------------------------------------------------------------


#: Where browser-use has kept its browser-configuration surface, newest first.
#: 0.13 dropped the top-level ``BrowserConfig`` in favour of ``BrowserProfile``
#: (the declarative half) and ``BrowserSession`` (the live half); older releases
#: only had ``BrowserConfig``. Probing all three keeps the answer correct across
#: the whole supported range instead of silently reporting "unsupported" the
#: moment upstream renames a class.
_BROWSER_USE_CONFIG_CLASSES: tuple[tuple[str, str], ...] = (
    ("browser_use.browser.profile", "BrowserProfile"),
    ("browser_use.browser.session", "BrowserSession"),
    ("browser_use", "BrowserConfig"),
)


def browser_use_accepts(param: str) -> bool:
    """True if any browser-use config class accepts ``param``.

    Signature inspection, not a browser launch: the failure mode worth catching
    is "the installed version silently dropped this kwarg", and that is visible
    statically.
    """
    import importlib  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    for module_name, attr in _BROWSER_USE_CONFIG_CLASSES:
        try:
            module = importlib.import_module(module_name)
            cls = getattr(module, attr, None)
            if cls is None:
                continue
            if param in inspect.signature(cls).parameters:
                return True
        except Exception:  # noqa: S112 — see below
            # Deliberately silent. This loop walks *candidate* locations for a
            # class that has moved between releases, so two of the three
            # attempts failing is the normal path, not an anomaly. Logging each
            # miss would emit noise on every healthy install; the caller gets
            # the only answer that matters (False) when every candidate misses.
            continue
    return False


def crawl4ai_accepts(param: str) -> bool:
    """True if Crawl4AI's ``BrowserConfig`` accepts ``param``."""
    import inspect  # noqa: PLC0415

    try:
        from crawl4ai import BrowserConfig  # noqa: PLC0415

        return param in inspect.signature(BrowserConfig).parameters
    except Exception:
        return False


def user_data_dir_supported() -> bool:
    """Probe whether installed Crawl4AI *and* browser-use accept ``user_data_dir``.

    Signature inspection only (no browser launch) — the failure mode we care
    about is "library version silently dropped the kwarg." If either lib is
    uninstalled, returns False with no error: the cascade still works without
    persistence, Pattern D just doesn't share its CF clearance forward.

    Historical note worth keeping: this probe used to import
    ``browser_use.BrowserConfig`` directly and return False when that failed.
    browser-use 0.13 — the version this project pins — removed that class, so
    the probe reported ``user_data_dir_unsupported`` on every correctly
    installed system, and ``/ready`` emitted a warning telling operators to
    upgrade libraries that were already new enough. Probing a list of candidate
    classes is what stops the next rename from doing the same thing.
    """
    if not agent_available():
        return False
    if not crawl4ai_accepts("user_data_dir"):
        return False
    return browser_use_accepts("user_data_dir")


def render_tier_enabled() -> bool:
    """Render tier is on by default; ``SCRAPPER_TOOL_RENDER_TIER=0`` disables it.

    Lives here rather than in ``http_server`` because it is a plain environment
    read that both the sidecar and ``doctor`` need, and ``doctor`` must not
    import the ``[http]``-gated module to answer it.
    """
    raw = os.environ.get("SCRAPPER_TOOL_RENDER_TIER")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def check_browser_module(browser: str) -> str:  # noqa: PLR0911 — one return per backend
    """Best-effort ``'ok'`` / ``'missing'`` / ``'unknown'`` for a backend's Python module."""
    if browser == "patchright":
        try:
            import patchright  # noqa: F401, PLC0415

            return "ok"
        except ImportError:
            return "missing"
    if browser == "camoufox":
        try:
            import camoufox  # noqa: F401, PLC0415

            return "ok"
        except ImportError:
            return "missing"
    if browser == "scrapling":
        try:
            import scrapling  # noqa: F401, PLC0415

            return "ok"
        except ImportError:
            return "missing"
    if browser == "obscura":
        # Obscura needs Playwright (to connect over CDP) plus a reachable
        # external server. The module check just verifies the client lib.
        try:
            import playwright  # noqa: F401, PLC0415

            return "ok"
        except ImportError:
            return "missing"
    return "unknown"


async def probe_llm(cfg: Any) -> tuple[bool | None, bool | None]:
    """Probe the configured LLM endpoint. Returns ``(reachable, model_available)``.

    Returns ``(None, None)`` for backends we can't probe (``llama_cpp`` /
    ``vllm``). Delegates to the agent-layer backend probes so auth headers,
    endpoint paths, and model-availability logic live in one place.
    """
    if cfg.llm in {"llama_cpp", "vllm"}:
        return None, None

    try:
        from scrapper_tool.agent.backends.llm import get_llm_backend  # noqa: PLC0415
        from scrapper_tool.errors import AgentLLMError  # noqa: PLC0415
    except ImportError:
        return None, None

    try:
        await get_llm_backend(cfg).probe()
        return True, True
    except AgentLLMError as exc:
        if "unreachable" in str(exc).lower():
            return False, False
        return True, False
    except Exception:
        return False, False
