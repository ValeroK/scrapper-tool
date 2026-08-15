"""Contract tests: a configured proxy actually reaches the thing that fetches.

A residential egress is the single change that would move every blocked row in
the cascade matrix — the four sites that stay walled return byte-identical
responses headless and headful, so the decision is made on IP before any local
lever applies. `config.proxy` has been wired through the ladder, the render tier
and both agent tiers for some time, and **not one test exercised it**.

That is the dangerous shape: the feature everyone would reach for first, silently
plumbed. If a launch-kwarg rename dropped it, every request would quietly go out
on the bare IP and look exactly like "the proxy did not help".

These assert the handoff, not the network. Proving traffic really egresses
elsewhere needs a live proxy and belongs in `scripts/e2e/` — see the note at the
bottom of this module.
"""

from __future__ import annotations

import contextlib
import sys
import types
from typing import Any

import pytest

from scrapper_tool.agent.backends import get_browser_backend
from scrapper_tool.agent.backends.browser import BrowserLaunchOptions
from scrapper_tool.agent.backends.fingerprint import NoOpGenerator

_PROXY = "http://user:pass@proxy.example:8080"


class _Behavior:
    async def apply(self, *_: Any, **__: Any) -> None: ...


class TestCamoufoxProxy:
    @pytest.mark.asyncio
    async def test_proxy_reaches_the_camoufox_launch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class _AsyncCamoufox:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def __aenter__(self) -> Any:
                return object()

            async def __aexit__(self, *_: object) -> None: ...

        fake = types.ModuleType("camoufox.async_api")
        fake.AsyncCamoufox = _AsyncCamoufox  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
        monkeypatch.setitem(sys.modules, "camoufox.async_api", fake)

        handle = await get_browser_backend("camoufox").launch(
            options=BrowserLaunchOptions(proxy=_PROXY),
            fingerprint=NoOpGenerator(),
            behavior=_Behavior(),
        )
        await handle.close()

        assert captured.get("proxy") == {"server": _PROXY}, (
            "Camoufox launched without the configured proxy — every request would "
            "egress on the bare IP while appearing configured"
        )

    @pytest.mark.asyncio
    async def test_no_proxy_key_when_none_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Absent, not `{"server": None}` — Playwright rejects the latter."""
        captured: dict[str, Any] = {}

        class _AsyncCamoufox:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def __aenter__(self) -> Any:
                return object()

            async def __aexit__(self, *_: object) -> None: ...

        fake = types.ModuleType("camoufox.async_api")
        fake.AsyncCamoufox = _AsyncCamoufox  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
        monkeypatch.setitem(sys.modules, "camoufox.async_api", fake)

        handle = await get_browser_backend("camoufox").launch(
            options=BrowserLaunchOptions(),
            fingerprint=NoOpGenerator(),
            behavior=_Behavior(),
        )
        await handle.close()
        assert "proxy" not in captured


class TestLadderProxy:
    @pytest.mark.asyncio
    async def test_proxy_reaches_the_curl_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ladder is the cheapest tier, so it is the one a proxy helps most.

        The proxy is handed to the curl_cffi **Session constructor**, not to a
        per-request call — patching the request function instead shows a
        misleading `None` and would have been read as "the ladder drops the
        proxy".
        """
        from scrapper_tool import ladder as ladder_mod

        seen: list[Any] = []

        class _FakeSession:
            def __init__(self, **kwargs: Any) -> None:
                seen.append(kwargs.get("proxy"))
                self.cookies: dict[str, Any] = {}

            async def request(self, *args: Any, **kwargs: Any) -> Any:
                class _Resp:
                    status_code = 200
                    text = "<html><head><title>ok</title></head><body>fine</body></html>"
                    headers: dict[str, str] = {}
                    content = b"x"
                    url = "https://target.example/"

                return _Resp()

            async def close(self) -> None: ...

        monkeypatch.setattr(ladder_mod, "_CurlCffiAsyncSession", _FakeSession)
        with contextlib.suppress(Exception):
            await ladder_mod.request_with_ladder(
                "GET", "https://target.example/", proxy=_PROXY, timeout=5
            )
        assert seen, "the ladder never built a session"
        assert seen[0] == _PROXY, f"ladder dropped the proxy: {seen}"


class TestProxyResolution:
    def test_pinned_proxy_wins_over_the_pool(self) -> None:
        """An explicitly configured proxy must not be silently swapped for a pooled one."""
        from scrapper_tool.proxy import resolve_proxy

        chosen, pool = resolve_proxy(None, _PROXY)
        assert chosen == _PROXY
        assert pool is None

    def test_no_proxy_and_no_pool_is_direct(self) -> None:
        from scrapper_tool.proxy import resolve_proxy

        chosen, pool = resolve_proxy(None, None)
        assert chosen is None
        assert pool is None


# Not covered here, deliberately: that traffic *actually* egresses via the proxy.
# That needs a live proxy and an IP echo, so it lives in scripts/e2e/ alongside
# the other checks that need a real network. Asserting the handoff catches the
# regression that would otherwise be invisible; asserting the egress needs
# infrastructure this suite must not depend on.
