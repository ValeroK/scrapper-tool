"""Unit tests for ``scrapper_tool.patterns.d`` — Scrapling Pattern D helper.

Scrapling brings ~400 MB of Playwright bloat, so it's an opt-in extra
(``pip install scrapper-tool[hostile]``). The unit tests here run
**without** Scrapling installed — they verify:

- Importing ``scrapper_tool.patterns.d`` succeeds (module-level
  docstring is reachable even when the extra is absent).
- Calling ``hostile_client()`` without Scrapling installed raises
  ``ImportError`` with the install hint.
- Calling ``hostile_client()`` *with* Scrapling installed (mocked)
  yields the fetcher and closes it on exit.

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


class TestHostileClientWithMockedScrapling:
    @pytest.mark.asyncio
    async def test_yields_fetcher_and_closes_on_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mock the scrapling.fetchers module before the lazy import fires.
        fake_fetcher = MagicMock()
        fake_fetcher.aclose = AsyncMock()
        fake_stealthy_fetcher_cls = MagicMock(return_value=fake_fetcher)

        fake_module = MagicMock()
        fake_module.StealthyFetcher = fake_stealthy_fetcher_cls

        fake_pkg = MagicMock()
        fake_pkg.fetchers = fake_module

        monkeypatch.setitem(sys.modules, "scrapling", fake_pkg)
        monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_module)

        async with hostile_client(headless=True, block_resources=True) as fetcher:
            assert fetcher is fake_fetcher

        # StealthyFetcher was constructed with our defaults.
        fake_stealthy_fetcher_cls.assert_called_once()
        kwargs = fake_stealthy_fetcher_cls.call_args.kwargs
        assert kwargs == {"headless": True, "block_resources": True}

        # aclose() was awaited on exit.
        fake_fetcher.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_extra_kwargs_propagate_to_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_fetcher = MagicMock()
        fake_fetcher.aclose = AsyncMock()
        fake_stealthy_fetcher_cls = MagicMock(return_value=fake_fetcher)

        fake_module = MagicMock()
        fake_module.StealthyFetcher = fake_stealthy_fetcher_cls

        fake_pkg = MagicMock()
        fake_pkg.fetchers = fake_module

        monkeypatch.setitem(sys.modules, "scrapling", fake_pkg)
        monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_module)

        async with hostile_client(
            extra_kwargs={"profile_dir": "/tmp/scrapling-profile"},
        ):
            pass

        kwargs = fake_stealthy_fetcher_cls.call_args.kwargs
        assert kwargs["profile_dir"] == "/tmp/scrapling-profile"
        assert kwargs["headless"] is True  # default preserved

    @pytest.mark.asyncio
    async def test_falls_back_to_sync_close_when_no_aclose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Older Scrapling versions expose ``close()`` (non-async).
        fake_fetcher = MagicMock()
        fake_fetcher.aclose = None  # signal "no async close"
        fake_fetcher.close = MagicMock()
        fake_stealthy_fetcher_cls = MagicMock(return_value=fake_fetcher)

        fake_module = MagicMock()
        fake_module.StealthyFetcher = fake_stealthy_fetcher_cls

        fake_pkg = MagicMock()
        fake_pkg.fetchers = fake_module

        monkeypatch.setitem(sys.modules, "scrapling", fake_pkg)
        monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_module)

        async with hostile_client():
            pass

        fake_fetcher.close.assert_called_once()


class TestModuleDocstring:
    def test_module_docstring_explains_pattern_d(self) -> None:
        # Even without scrapling installed, the docstring should be
        # readable — that's the pedagogical reason to ship the helper
        # behind a lazy import rather than a hard module-level import.
        assert patterns_d.__doc__ is not None
        assert "Cloudflare Turnstile" in patterns_d.__doc__
        assert "scrapper-tool[hostile]" in patterns_d.__doc__
