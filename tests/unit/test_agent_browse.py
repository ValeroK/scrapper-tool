"""Unit tests for ``scrapper_tool.agent.browse`` (Pattern E2).

Mocks the entire browser-use stack and the chosen browser backend so
the test suite runs in the default ``[dev,agent]`` install. The
contract we exercise:

- LLM probe runs first — Ollama-down fails fast.
- Browser backend's ``launch`` is called with the right kwargs and
  closed even on error.
- ``AgentHistoryList`` (from a fixture) is converted into
  ``AgentResult`` with screenshots downsampled and DOM snippets capped.
- Schema validation populates ``error="schema-validation-failed"``
  without raising.
- Block-detection signals propagate as ``AgentBlockedError``.
- Timeouts surface as ``AgentTimeoutError``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from scrapper_tool.agent import browse as browse_mod
from scrapper_tool.agent.backends.browser import BrowserHandle
from scrapper_tool.agent.types import AgentConfig
from scrapper_tool.errors import AgentBlockedError, AgentTimeoutError

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "agent"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeHistoryItem:
    """Mirrors a single browser-use history step."""

    def __init__(self, **fields: Any) -> None:
        self.step = fields.get("step")
        self.model_action = fields.get("model_action")
        self.url = fields.get("url")
        self.selector = fields.get("selector")
        self.screenshot = fields.get("screenshot")
        self.extracted_content = fields.get("extracted_content")


class _FakeAgentHistoryList:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.history = [_FakeHistoryItem(**h) for h in payload["history"]]
        self.url = payload.get("final_url")
        self._final = payload.get("final_result")
        self.total_input_tokens = payload.get("total_input_tokens", 0)

    def final_result(self) -> Any:
        return self._final


@pytest.fixture
def history_fixture() -> _FakeAgentHistoryList:
    payload = json.loads((_FIXTURES / "history_replay.json").read_text())
    return _FakeAgentHistoryList(payload)


@pytest.fixture
def fake_browser_use(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub out the browser-use SDK with a controllable Agent class."""
    mod = types.ModuleType("browser_use")

    class _BrowserConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class _BUBrowser:
        def __init__(self, config: Any | None = None) -> None:
            self.config = config
            self.playwright_browser: Any = None

        async def close(self) -> None:
            return None

    class _Agent:
        last_kwargs: dict[str, Any] = {}
        next_history: Any = None
        next_exception: Exception | None = None

        def __init__(self, **kwargs: Any) -> None:
            type(self).last_kwargs = kwargs

        async def run(self, *, max_steps: int | None = None) -> Any:
            type(self).last_kwargs["max_steps"] = max_steps
            if type(self).next_exception is not None:
                raise type(self).next_exception
            return type(self).next_history

    mod.Agent = _Agent  # type: ignore[attr-defined]
    mod.Browser = _BUBrowser  # type: ignore[attr-defined]
    mod.BrowserConfig = _BrowserConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "browser_use", mod)
    return {"agent_cls": _Agent, "browser_cls": _BUBrowser}


@pytest.fixture
def fake_handle(monkeypatch: pytest.MonkeyPatch) -> BrowserHandle:
    """A BrowserHandle wrapping a sentinel object — caller never touches it."""
    closed = {"flag": False}

    async def shutdown() -> None:
        closed["flag"] = True

    handle = BrowserHandle(
        name="patchright",
        playwright_browser=object(),
        raw=object(),
        shutdown=shutdown,
    )

    # Wire every backend's launch() to return this handle.
    async def fake_launch(
        self: Any, *, headful: bool, proxy: str | None, fingerprint: Any, behavior: Any
    ) -> BrowserHandle:
        return handle

    from scrapper_tool.agent.backends import browser as browser_mod

    for cls in (
        browser_mod.CamoufoxBackend,
        browser_mod.PatchrightBackend,
        browser_mod.ScraplingBackend,
    ):
        monkeypatch.setattr(cls, "launch", fake_launch)

    handle._closed = closed  # type: ignore[attr-defined]
    return handle


@pytest.fixture(autouse=True)
def _patch_llm_probe(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    probe = AsyncMock(return_value=None)
    from scrapper_tool.agent.backends.llm import OllamaBackend

    monkeypatch.setattr(OllamaBackend, "probe", probe)
    monkeypatch.setattr(OllamaBackend, "to_browser_use_llm", lambda self: object())
    return probe


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class _BrowseSchema(BaseModel):
    title: str


class TestRunBrowseHappyPath:
    @pytest.mark.asyncio
    async def test_history_converts_to_agent_result(
        self,
        fake_browser_use: dict[str, Any],
        fake_handle: BrowserHandle,
        history_fixture: _FakeAgentHistoryList,
    ) -> None:
        fake_browser_use["agent_cls"].next_history = history_fixture
        cfg = AgentConfig(browser="patchright", captcha_solver="none", max_steps=10, timeout_s=30)
        result = await browse_mod.run_browse(
            "https://example.com", "Find more info and return the title", config=cfg
        )

        assert result.mode == "browse"
        assert result.final_url == "https://example.com/more"
        assert result.steps_used == 3
        assert result.tokens_used == 4321
        # final_result is JSON; gets parsed to dict.
        assert result.data == {"title": "More Information..."}
        # Browser handle should have been closed.
        assert fake_handle._closed["flag"] is True  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_schema_validation_failure_does_not_raise(
        self,
        fake_browser_use: dict[str, Any],
        fake_handle: BrowserHandle,
        history_fixture: _FakeAgentHistoryList,
    ) -> None:
        # The fixture's final_result has only "title" — a stricter
        # schema requiring "price" should fail validation.
        class Stricter(BaseModel):
            title: str
            price: float

        fake_browser_use["agent_cls"].next_history = history_fixture
        cfg = AgentConfig(browser="patchright", captcha_solver="none", max_steps=10, timeout_s=30)
        result = await browse_mod.run_browse(
            "https://example.com", "...", config=cfg, schema=Stricter
        )
        assert result.error and "schema-validation-failed" in result.error
        assert isinstance(result.data, dict)
        assert "_raw" in result.data


class TestRunBrowseFailures:
    @pytest.mark.asyncio
    async def test_blocked_signal_in_exception_propagates(
        self, fake_browser_use: dict[str, Any], fake_handle: BrowserHandle
    ) -> None:
        fake_browser_use["agent_cls"].next_exception = RuntimeError(
            "Detected Cloudflare challenge — gave up"
        )
        cfg = AgentConfig(browser="patchright", captcha_solver="none")
        with pytest.raises(AgentBlockedError, match="blocked"):
            await browse_mod.run_browse("https://e.com", "x", config=cfg)
        # Handle still closed even on raise.
        assert fake_handle._closed["flag"] is True  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_timeout_maps_to_agent_timeout(
        self,
        fake_browser_use: dict[str, Any],
        fake_handle: BrowserHandle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_wait_for(coro: Any, timeout: float) -> Any:
            import contextlib

            with contextlib.suppress(Exception):
                coro.close()
            raise TimeoutError("simulated")

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        cfg = AgentConfig(browser="patchright", captcha_solver="none", timeout_s=0.1)
        with pytest.raises(AgentTimeoutError):
            await browse_mod.run_browse("https://e.com", "x", config=cfg)

    @pytest.mark.asyncio
    async def test_zendriver_unsupported_for_browse(
        self, fake_browser_use: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Zendriver returns a handle with playwright_browser=None.
        async def shutdown() -> None:
            return None

        handle = BrowserHandle(
            name="zendriver",
            playwright_browser=None,
            raw=object(),
            shutdown=shutdown,
        )

        async def launch_zen(self: Any, **_: Any) -> BrowserHandle:
            return handle

        from scrapper_tool.agent.backends.browser import ZendriverBackend

        monkeypatch.setattr(ZendriverBackend, "launch", launch_zen)

        cfg = AgentConfig(browser="zendriver", captcha_solver="none")
        with pytest.raises(Exception, match="Playwright Browser"):
            await browse_mod.run_browse("https://e.com", "x", config=cfg)


class TestBrowseHelpers:
    def test_action_label_falls_back(self) -> None:
        from scrapper_tool.agent.browse import _action_label

        # Empty item → fallback.
        assert _action_label(_FakeHistoryItem()) == "step"
        # Different attr names.
        assert _action_label(_FakeHistoryItem(model_action="click")) == "click"

    def test_action_target_picks_first_truthy(self) -> None:
        from scrapper_tool.agent.browse import _action_target

        assert _action_target(_FakeHistoryItem()) is None
        assert _action_target(_FakeHistoryItem(selector="#x")) == "#x"
        assert _action_target(_FakeHistoryItem(url="https://e.com")) == "https://e.com"

    def test_action_snippet_truncated_to_1k(self) -> None:
        from scrapper_tool.agent.browse import _action_snippet

        long = "x" * 5000
        item = _FakeHistoryItem(extracted_content=long)
        snippet = _action_snippet(item)
        assert snippet is not None
        assert len(snippet) <= 1024

    def test_extract_screenshot_bytes_handles_bytes_str_none(self) -> None:
        from scrapper_tool.agent.browse import _extract_screenshot_bytes

        assert _extract_screenshot_bytes(_FakeHistoryItem()) is None
        assert _extract_screenshot_bytes(_FakeHistoryItem(screenshot=b"raw-bytes")) == b"raw-bytes"
        # Base64 string round trip.
        import base64

        encoded = base64.b64encode(b"png").decode("ascii")
        assert _extract_screenshot_bytes(_FakeHistoryItem(screenshot=encoded)) == b"png"

    def test_coerce_final_handles_invalid_json_string(self) -> None:
        from scrapper_tool.agent.browse import _coerce_final

        data, err = _coerce_final("not-json", schema=None)
        assert err is None
        # Not-json string is preserved as _raw.
        assert isinstance(data, dict)
        assert data["_raw"] == "not-json"

    def test_coerce_final_passes_through_dict(self) -> None:
        from scrapper_tool.agent.browse import _coerce_final

        data, err = _coerce_final({"x": 1}, schema=None)
        assert data == {"x": 1}
        assert err is None

    def test_coerce_final_validates_pydantic(self) -> None:
        from scrapper_tool.agent.browse import _coerce_final

        class Schema(BaseModel):
            x: int

        data, err = _coerce_final({"x": 5}, schema=Schema)
        assert err is None
        assert data == {"x": 5}


class TestHistoryConversion:
    def test_no_match_when_final_is_none(self) -> None:
        from scrapper_tool.agent.browse import _history_to_agent_result

        history = _FakeAgentHistoryList(
            {"history": [], "final_url": "https://e.com", "final_result": None}
        )
        result = _history_to_agent_result(history, url="https://e.com", duration_s=1.0, schema=None)
        assert result.error == "no-match"
        assert result.data is None

    def test_blocked_detected_in_history_text(self) -> None:
        from scrapper_tool.agent.browse import _history_to_agent_result

        history = _FakeAgentHistoryList(
            {
                "history": [
                    {
                        "step": 1,
                        "model_action": "extract",
                        "url": "https://e.com",
                        "extracted_content": "Cloudflare blocked the request",
                    }
                ],
                "final_url": "https://e.com",
                "final_result": None,
            }
        )
        result = _history_to_agent_result(history, url="https://e.com", duration_s=0.1, schema=None)
        assert result.blocked is True

    def test_screenshots_downsampled_and_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Inject a fake PIL.Image to make downsample run deterministically.
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            pytest.skip("Pillow required for screenshot test")

        # Build a fake history with 5 PNG screenshots — only 3 should
        # survive the cap; each should be downsampled to ≤1024px wide.
        from PIL import Image as PILImage

        def png_bytes(width: int, height: int) -> bytes:
            buf = __import__("io").BytesIO()
            PILImage.new("RGB", (width, height), color=(255, 0, 0)).save(buf, format="PNG")
            return buf.getvalue()

        big = png_bytes(2048, 1200)
        history = _FakeAgentHistoryList(
            {
                "history": [
                    {"step": i, "model_action": "act", "url": "x", "screenshot": None}
                    for i in range(1, 6)
                ],
                "final_url": "x",
                "final_result": None,
            }
        )
        # Manually attach screenshots after construction.
        for item in history.history:
            item.screenshot = big

        from scrapper_tool.agent.browse import _history_to_agent_result

        result = _history_to_agent_result(history, url="x", duration_s=0.1, schema=None)
        assert result.screenshots is not None
        assert len(result.screenshots) == 3
        # Verify downsample: re-decode each PNG and check width.
        import io as _io

        for png in result.screenshots:
            img = PILImage.open(_io.BytesIO(png))
            assert img.width <= 1024
