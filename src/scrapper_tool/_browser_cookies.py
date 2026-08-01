"""The impure half of the cookie feature: reading a live browser cookie store.

This module is deliberately tiny and deliberately dumb. It locates a backend,
calls it, and hands back raw rows. **No normalization happens here** — that is
:func:`scrapper_tool.cookies.from_browser_store`'s job, because normalization is
logic and logic belongs where the tests are. What is left is the one operation
that genuinely cannot be unit-tested: talking to the OS credential store.

Why this can't run in a container
---------------------------------
Reading a browser cookie store needs the platform's credential store — macOS
Keychain, Windows DPAPI, Linux gnome-keyring/KWallet — plus a real browser
profile on disk. A Linux container has neither. This is physical, not a
configuration gap, which is why extraction is host-side only and the CLI says so
instead of failing obscurely.

Backends
--------
``rookiepy`` (MIT) is the declared ``[cookies]`` extra. ``browser_cookie3`` is
**LGPL** and must never enter this project's dependency tree, so it is only ever
used if the user already has it — discovered via ``find_spec``, never declared,
never in ``uv.lock``. ``tests/unit/test_cookies.py`` asserts it appears in no
dependency list.
"""

from __future__ import annotations

from typing import Any

__all__ = ["BrowserCookieError", "read_browser_cookies", "resolve_backend"]

#: Browsers we try, in order, when the caller doesn't name one. Names match
#: rookiepy's module-level functions (verified against 0.5.6).
#:
#: Firefox and its forks lead deliberately: Firefox is the only browser reliable
#: on all three platforms — no Keychain prompt on macOS, no App-Bound Encryption
#: on Windows, and an unencrypted store on Linux. Chromium-based browsers come
#: after because each has a platform where it needs a prompt or admin rights.
#:
#: Safari is absent because rookiepy has no Safari reader, and its cookie
#: directory is TCC-protected anyway — reading it needs Full Disk Access, which
#: a CLI cannot grant itself.
_BROWSER_ORDER = (
    "firefox",
    "librewolf",
    "chrome",
    "chromium",
    "brave",
    "edge",
    "vivaldi",
    "opera",
    "opera_gx",
    "arc",
)


class BrowserCookieError(RuntimeError):
    """Raised when no backend is installed, or a backend refuses to read."""


def resolve_backend() -> Any:
    """Import and return the cookie backend module, or raise.

    Prefers rookiepy; falls back to an already-present browser_cookie3.
    """
    try:
        import rookiepy  # noqa: PLC0415

        return rookiepy
    except ImportError:
        pass

    from importlib.util import find_spec  # noqa: PLC0415

    try:
        if find_spec("browser_cookie3") is not None:
            import browser_cookie3  # noqa: PLC0415

            return browser_cookie3
    except (ImportError, ValueError):
        pass

    msg = (
        "No browser-cookie backend installed. Install the extra:\n"
        "    pip install 'scrapper-tool[cookies]'"
    )
    raise BrowserCookieError(msg)


def read_browser_cookies(
    domain: str, *, browser: str | None = None, backend: Any = None
) -> list[dict[str, Any]]:
    """Read raw cookie rows for ``domain`` from a local browser profile.

    ``backend`` is injectable so tests can pass a fake without touching a real
    browser; production callers leave it None. Returns raw dicts — see the
    module docstring for why nothing is normalized here.
    """
    module = backend if backend is not None else resolve_backend()
    names = (browser,) if browser else _BROWSER_ORDER

    errors: list[str] = []
    for name in names:
        reader = getattr(module, name, None)
        if reader is None:
            continue
        try:
            rows = reader([domain])
        except Exception as exc:
            errors.append(f"{name}: {_first_line(exc)}")
            continue
        return [dict(row) for row in rows]

    if errors:
        # One line per backend. rookiepy surfaces multi-line Rust panics with
        # source locations; pasting ten of those verbatim buries the one fact
        # the user needs, which is "no browser profile was found here".
        detail = "\n  ".join(errors)
        msg = f"could not read any browser profile.\n  {detail}"
        raise BrowserCookieError(msg)
    msg = f"no usable browser backend among: {', '.join(names)}"
    raise BrowserCookieError(msg)


def _first_line(exc: Exception, *, limit: int = 120) -> str:
    """Collapse a backend exception to one readable line."""
    text = str(exc).strip().splitlines()
    head = text[0].strip() if text else exc.__class__.__name__
    return head if len(head) <= limit else head[: limit - 1] + "…"
