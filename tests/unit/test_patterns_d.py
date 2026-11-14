"""Unit tests for ``scrapper_tool.patterns.d`` — Scrapling Pattern D helper.

Scrapling brings ~400 MB of Playwright bloat, so it's an opt-in extra
(``pip install scrapper-tool[hostile]``). The unit tests here run
**without** Scrapling installed — they verify:

- Importing ``scrapper_tool.patterns.d`` succeeds (module-level
  docstring is reachable even when the extra is absent).
- Calling ``hostile_client()`` without Scrapling installed raises
  ``ImportError`` with the install hint.
- Calling ``hostile_client()`` *with* Scrapling installed (mocked)
  sends every option on Scrapling's ``async_fetch`` classmethod.

Real Scrapling integration is exercised by an opt-in live test
(``tests/integration/test_live_probes.py``, marker ``live``); CI's
default profile skips it.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapper_tool.patterns import d as patterns_d
from scrapper_tool.patterns.d import hostile_client


class TestHostileClientWithoutScrapling:
    @pytest.mark.asyncio
    async def test_raises_import_error_when_extra_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the lazy import to fail. Any pre-imported scrapling
        # modules need to be evicted; the most reliable way is to
        # blacklist them from sys.modules and patch the import.
        for mod_name in list(sys.modules):
            if mod_name.startswith("scrapling"):
                monkeypatch.delitem(sys.modules, mod_name, raising=False)

        # Block any further import of scrapling.
        real_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )

        def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("scrapling"):
                raise ImportError(f"Mocked: {name} not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _blocked_import)

        with pytest.raises(ImportError) as excinfo:
            async with hostile_client():
                pass  # pragma: no cover — never reached

        msg = str(excinfo.value)
        assert "scrapper-tool[hostile]" in msg
        assert "Playwright" in msg


def _install_fake_scrapling(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Install a ``scrapling.fetchers`` shaped like the real one; return its fetch mock.

    Real Scrapling (0.3+) takes every option on the ``async_fetch`` classmethod
    and makes ``StealthyFetcher(...)`` a warning-only no-op. The fake class
    therefore refuses construction outright: an option sent to ``__init__`` is
    an option Scrapling throws away.
    """
    fetch = AsyncMock(return_value=MagicMock(status=200))

    class _FakeStealthyFetcher:
        def __init__(self, *_a: Any, **kwargs: Any) -> None:
            msg = f"StealthyFetcher() ignores its options: {kwargs}"
            raise AssertionError(msg)

        async_fetch = fetch

    fake_module = MagicMock()
    fake_module.StealthyFetcher = _FakeStealthyFetcher
    fake_pkg = MagicMock()
    fake_pkg.fetchers = fake_module
    monkeypatch.setitem(sys.modules, "scrapling", fake_pkg)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_module)
    return fetch


class TestHostileClientWithMockedScrapling:
    @pytest.mark.asyncio
    async def test_options_ride_on_every_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """headless, resource blocking and the timeout reach Scrapling's fetch.

        Until 4.6.0 they went to ``StealthyFetcher.__init__`` and were dropped.
        Scrapling's timeout is in milliseconds; ours is seconds.
        """
        fetch = _install_fake_scrapling(monkeypatch)

        async with hostile_client(timeout=12.5, headless=False, block_resources=False) as fetcher:
            await fetcher.async_fetch("https://example.test/p", solve_cloudflare=True)

        fetch.assert_awaited_once()
        assert fetch.call_args.args == ("https://example.test/p",)
        assert fetch.call_args.kwargs == {
            "headless": False,
            "disable_resources": False,
            "timeout": 12500,
            "solve_cloudflare": True,
        }

    @pytest.mark.asyncio
    async def test_extra_kwargs_propagate_to_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fetch = _install_fake_scrapling(monkeypatch)

        async with hostile_client(extra_kwargs={"user_data_dir": "/tmp/scrapling-profile"}) as f:
            await f.async_fetch("https://example.test/p")

        kwargs = fetch.call_args.kwargs
        assert kwargs["user_data_dir"] == "/tmp/scrapling-profile"
        assert kwargs["headless"] is True  # default preserved
        assert kwargs["disable_resources"] is True

    @pytest.mark.asyncio
    async def test_a_per_call_kwarg_overrides_the_client_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = _install_fake_scrapling(monkeypatch)

        async with hostile_client(headless=True) as fetcher:
            await fetcher.async_fetch("https://example.test/p", headless=False)

        assert fetch.call_args.kwargs["headless"] is False


class TestHostileClientAgainstRealScrapling:
    """The fakes above only help if they match the installed library."""

    def test_async_fetch_is_a_classmethod_accepting_our_options(self) -> None:
        import inspect
        import typing

        fetchers = pytest.importorskip("scrapling.fetchers")
        from scrapling.engines._browsers._types import StealthSession

        cls = fetchers.StealthyFetcher
        assert isinstance(inspect.getattr_static(cls, "async_fetch"), classmethod)
        accepted = set(typing.get_type_hints(StealthSession))
        sent = {
            "headless",
            "disable_resources",
            "timeout",
            "cookies",
            "solve_cloudflare",
            "network_idle",
            "user_data_dir",
        }
        assert sent <= accepted, f"Scrapling no longer accepts: {sorted(sent - accepted)}"


class TestModuleDocstring:
    def test_module_docstring_explains_pattern_d(self) -> None:
        # Even without scrapling installed, the docstring should be
        # readable — that's the pedagogical reason to ship the helper
        # behind a lazy import rather than a hard module-level import.
        assert patterns_d.__doc__ is not None
        assert "Cloudflare Turnstile" in patterns_d.__doc__
        assert "scrapper-tool[hostile]" in patterns_d.__doc__
