"""Unit tests for the agent backend resolvers + adapters.

Each backend is exercised through its public resolver. Heavy deps
(camoufox, patchright, browser-use, browserforge) are mocked — these
tests must run in the default ``[dev,agent]`` install.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from scrapper_tool.agent.backends import (
    BrowserLaunchOptions,
    CamoufoxBackend,
    ObscuraBackend,
    PatchrightBackend,
    ScraplingBackend,
    get_behavior_policy,
    get_browser_backend,
    get_captcha_solver,
    get_fingerprint_generator,
    get_llm_backend,
    is_vision_model,
    supports_vision,
)
from scrapper_tool.agent.backends import llm as llm_mod
from scrapper_tool.agent.backends.fingerprint import (
    BrowserforgeGenerator,
    NoOpGenerator,
)
from scrapper_tool.agent.backends.llm import (
    OllamaBackend,
    OpenAICompatBackend,
)
from scrapper_tool.agent.types import AgentConfig
from scrapper_tool.errors import AgentLLMError, ConfigurationError

# --- Browser resolver -----------------------------------------------------


class TestBrowserResolver:
    def test_default_is_camoufox(self) -> None:
        backend = get_browser_backend("camoufox")
        assert isinstance(backend, CamoufoxBackend)
        assert backend.name == "camoufox"

    def test_each_named_backend(self) -> None:
        cases: dict[str, type] = {
            "camoufox": CamoufoxBackend,
            "patchright": PatchrightBackend,
            "scrapling": ScraplingBackend,
        }
        for name, cls in cases.items():
            assert isinstance(get_browser_backend(name), cls)

    def test_pruned_backends_are_rejected(self) -> None:
        # Zendriver / Botasaurus were removed in v1.5.0 — they must no
        # longer resolve (they raised AgentError at browse time anyway).
        for name in ("zendriver", "botasaurus"):
            with pytest.raises(ConfigurationError, match="Unknown browser backend"):
                get_browser_backend(name)

    def test_obscura_resolves_with_cdp_url(self) -> None:
        backend = get_browser_backend("obscura", cdp_url="ws://host:9999")
        assert isinstance(backend, ObscuraBackend)
        assert backend.name == "obscura"
        assert backend._cdp_url == "ws://host:9999"

    def test_obscura_default_cdp_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL", raising=False)
        backend = get_browser_backend("obscura")
        assert isinstance(backend, ObscuraBackend)
        assert backend._cdp_url == "http://127.0.0.1:9222"

    async def test_obscura_launch_connects_over_cdp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Inject a fake playwright.async_api so launch() runs without a
        # real server (kwargs-contract test — catches connect_over_cdp drift).
        from unittest.mock import AsyncMock

        browser = type("B", (), {"close": AsyncMock(), "new_context": AsyncMock()})()
        connect = AsyncMock(return_value=browser)

        class _Ctx:
            async def __aenter__(self) -> Any:
                return type(
                    "PW", (), {"chromium": type("C", (), {"connect_over_cdp": connect})()}
                )()

            async def __aexit__(self, *a: Any) -> None:
                return None

        fake_api = types.ModuleType("playwright.async_api")
        fake_api.async_playwright = _Ctx  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
        monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)

        backend = get_browser_backend("obscura", cdp_url="ws://host:9999")
        handle = await backend.launch(
            options=BrowserLaunchOptions(),
            fingerprint=get_fingerprint_generator("none"),
            behavior=get_behavior_policy("off"),
        )
        connect.assert_awaited_once_with("ws://host:9999")
        assert handle.playwright_browser is browser
        assert handle.name == "obscura"
        await handle.close()
        browser.close.assert_awaited_once()

    async def test_obscura_launch_connect_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        connect = AsyncMock(side_effect=RuntimeError("no server"))

        class _Ctx:
            async def __aenter__(self) -> Any:
                return type(
                    "PW", (), {"chromium": type("C", (), {"connect_over_cdp": connect})()}
                )()

            async def __aexit__(self, *a: Any) -> None:
                return None

        fake_api = types.ModuleType("playwright.async_api")
        fake_api.async_playwright = _Ctx  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
        monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)

        backend = get_browser_backend("obscura", cdp_url="ws://dead:1")
        with pytest.raises(ImportError, match="Obscura CDP connect failed"):
            await backend.launch(
                options=BrowserLaunchOptions(),
                fingerprint=get_fingerprint_generator("none"),
                behavior=get_behavior_policy("off"),
            )

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="Unknown browser backend"):
            get_browser_backend("internet-explorer")

    def test_camoufox_install_error_message_is_helpful(self) -> None:
        # We can't drive `.launch()` in unit tests (real Camoufox needed),
        # so verify the error message at the module level.
        from scrapper_tool.agent.backends import browser as browser_mod

        assert "[llm-agent]" in browser_mod._CAMOUFOX_NOT_INSTALLED
        assert "camoufox fetch" in browser_mod._CAMOUFOX_NOT_INSTALLED
        assert "[llm-agent]" in browser_mod._PATCHRIGHT_NOT_INSTALLED
        assert "[hostile]" in browser_mod._SCRAPLING_NOT_INSTALLED


# --- Backend launch() kwargs contracts (API-drift protection) -------------


class TestBackendLaunchContracts:
    """Exercise each launch() against a mocked lazy dependency and assert the
    exact kwargs handed to it. These fail loudly if camoufox / patchright /
    scrapling rename or drop a launch parameter on upgrade.
    """

    async def test_camoufox_launch_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class _AsyncCamoufox:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def __aenter__(self) -> Any:
                return object()

            async def __aexit__(self, *a: Any) -> None:
                return None

        fake = types.ModuleType("camoufox.async_api")
        fake.AsyncCamoufox = _AsyncCamoufox  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
        monkeypatch.setitem(sys.modules, "camoufox.async_api", fake)

        handle = await CamoufoxBackend().launch(
            options=BrowserLaunchOptions(headful=False, proxy="http://p:8080"),
            fingerprint=get_fingerprint_generator("none"),
            behavior=get_behavior_policy("off"),
        )
        assert captured["headless"] is True
        assert captured["humanize"] is True
        assert captured["geoip"] is True
        assert captured["proxy"] == {"server": "http://p:8080"}
        assert handle.name == "camoufox"
        # Knobs left at their defaults must NOT be passed at all.
        assert "user_data_dir" not in captured
        assert "block_images" not in captured
        await handle.close()

    async def test_camoufox_render_knobs_reach_asynccamoufox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v1.6.0 knobs: virtual display, persistent profile, image blocking.

        The persistent-profile assertion is the regression guard for the bug where
        ``user_data_dir`` was threaded through the cascade but never reached Camoufox,
        silently breaking cf_clearance carry-forward for E2.
        """
        captured: dict[str, Any] = {}

        class _AsyncCamoufox:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def __aenter__(self) -> Any:
                return object()

            async def __aexit__(self, *a: Any) -> None:
                return None

        fake = types.ModuleType("camoufox.async_api")
        fake.AsyncCamoufox = _AsyncCamoufox  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
        monkeypatch.setitem(sys.modules, "camoufox.async_api", fake)

        handle = await CamoufoxBackend().launch(
            options=BrowserLaunchOptions(
                headless_mode="virtual",
                user_data_dir="/tmp/profile-x",
                block_images=True,
                fingerprint_preset=True,
                os="windows",
                locale="he-IL",
            ),
            fingerprint=get_fingerprint_generator("none"),
            behavior=get_behavior_policy("off"),
        )
        assert captured["headless"] == "virtual"
        assert captured["user_data_dir"] == "/tmp/profile-x"
        assert captured["persistent_context"] is True  # must accompany user_data_dir
        assert captured["block_images"] is True
        assert captured["fingerprint_preset"] is True
        assert captured["os"] == "windows"
        assert captured["locale"] == "he-IL"
        await handle.close()

    async def test_patchright_launch_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import AsyncMock

        launched: dict[str, Any] = {}
        browser = type("B", (), {"close": AsyncMock(), "new_context": AsyncMock()})()

        class _Chromium:
            async def launch(self, **kwargs: Any) -> Any:
                launched.update(kwargs)
                return browser

        class _Ctx:
            async def __aenter__(self) -> Any:
                return type("PW", (), {"chromium": _Chromium()})()

            async def __aexit__(self, *a: Any) -> None:
                return None

        fake = types.ModuleType("patchright.async_api")
        fake.async_playwright = _Ctx  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "patchright", types.ModuleType("patchright"))
        monkeypatch.setitem(sys.modules, "patchright.async_api", fake)

        handle = await PatchrightBackend().launch(
            options=BrowserLaunchOptions(headful=False, proxy="http://p:1"),
            fingerprint=get_fingerprint_generator("none"),
            behavior=get_behavior_policy("off"),
        )
        assert launched["headless"] is True
        assert launched["proxy"] == {"server": "http://p:1"}
        assert handle.playwright_browser is browser
        await handle.close()

    async def test_scrapling_launch_delegates_to_hostile_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entered: dict[str, Any] = {}

        class _Ctx:
            async def __aenter__(self) -> Any:
                return "fetcher"

            async def __aexit__(self, *a: Any) -> None:
                return None

        def fake_hostile_client(*, headless: bool) -> Any:
            entered["headless"] = headless
            return _Ctx()

        import scrapper_tool.patterns.d as d_mod

        monkeypatch.setattr(d_mod, "hostile_client", fake_hostile_client)

        handle = await ScraplingBackend().launch(
            options=BrowserLaunchOptions(),
            fingerprint=get_fingerprint_generator("none"),
            behavior=get_behavior_policy("off"),
        )
        assert entered["headless"] is True
        assert handle.playwright_browser is None
        assert handle.raw == "fetcher"
        await handle.close()


# --- Fingerprint resolver -------------------------------------------------


class TestFingerprintResolver:
    def test_default_is_browserforge(self) -> None:
        gen = get_fingerprint_generator("browserforge")
        assert isinstance(gen, BrowserforgeGenerator)

    def test_none_returns_noop(self) -> None:
        gen = get_fingerprint_generator("none")
        assert isinstance(gen, NoOpGenerator)
        fp = gen.generate()
        assert fp.user_agent == ""
        assert fp.viewport == (1280, 800)

    def test_unknown_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="Unknown fingerprint"):
            get_fingerprint_generator("evil")

    def test_browserforge_lazy_import_failure_is_helpful(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Insert sentinel modules that raise on import to force the
        # ImportError path.
        for mod in ("browserforge", "browserforge.fingerprints", "browserforge.headers"):
            monkeypatch.setitem(sys.modules, mod, None)
        gen = BrowserforgeGenerator()
        with pytest.raises(ImportError, match="\\[llm-agent\\]"):
            gen.generate()

    def test_browserforge_with_mocked_modules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Build minimal stubs that satisfy the API surface we use.
        fake_headers = types.ModuleType("browserforge.headers")

        class _HG:
            def __init__(self, **_: Any) -> None: ...

            def generate(self) -> dict[str, str]:
                return {"User-Agent": "Mozilla/5.0 (FakeOS) Chrome/130"}

        fake_headers.HeaderGenerator = _HG  # type: ignore[attr-defined]

        fake_fps = types.ModuleType("browserforge.fingerprints")

        class _FP:
            class screen:
                width = 1920
                height = 1080

            class navigator:
                language = "en-GB"

        class _FPG:
            def __init__(self, **_: Any) -> None: ...

            def generate(self) -> _FP:
                return _FP()

        fake_fps.FingerprintGenerator = _FPG  # type: ignore[attr-defined]

        fake_root = types.ModuleType("browserforge")
        monkeypatch.setitem(sys.modules, "browserforge", fake_root)
        monkeypatch.setitem(sys.modules, "browserforge.headers", fake_headers)
        monkeypatch.setitem(sys.modules, "browserforge.fingerprints", fake_fps)

        fp = BrowserforgeGenerator().generate()
        assert "Chrome" in fp.user_agent
        assert fp.viewport == (1920, 1080)
        assert fp.locale == "en-GB"


# --- Behavior resolver ----------------------------------------------------


class TestBehaviorResolver:
    def test_each_policy(self) -> None:
        for name in ("humanlike", "fast", "off"):
            assert get_behavior_policy(name).name == name

    def test_unknown_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="Unknown behavior policy"):
            get_behavior_policy("evil")

    @pytest.mark.asyncio
    async def test_humanlike_keystroke_distribution_in_bounds(self) -> None:
        policy = get_behavior_policy("humanlike")
        # 200 samples — assert no outlier escapes the clamp.
        samples = [await policy.shape_keystrokes() for _ in range(200)]
        assert all(0.025 <= s <= 0.6 for s in samples), f"clamp broken: {samples}"

    @pytest.mark.asyncio
    async def test_fast_policy_skips_delays(self) -> None:
        policy = get_behavior_policy("fast")
        assert (await policy.shape_keystrokes()) == 0.0
        assert (await policy.shape_scroll()) == 0.0
        assert policy.mouse_path((0, 0), (100, 100)) == []


# --- LLM resolver ---------------------------------------------------------


class TestLLMResolver:
    def test_ollama_is_default(self) -> None:
        cfg = AgentConfig()
        backend = get_llm_backend(cfg)
        assert isinstance(backend, OllamaBackend)
        assert backend.model == "qwen3-vl:8b"

    def test_openai_compat(self) -> None:
        cfg = AgentConfig(llm="openai_compat", ollama_url="http://localhost:8080")
        backend = get_llm_backend(cfg)
        assert isinstance(backend, OpenAICompatBackend)

    def test_openai_compat_with_api_key(self) -> None:
        from pydantic import SecretStr

        api_key = "sk-test-key-12345"
        cfg = AgentConfig(
            llm="openai_compat",
            ollama_url="http://localhost:8080",
            llm_api_key=SecretStr(api_key),
        )
        backend = get_llm_backend(cfg)
        assert isinstance(backend, OpenAICompatBackend)
        assert backend.api_key == api_key

    def test_unknown_raises(self) -> None:
        cfg = AgentConfig.model_construct(llm="evil")  # bypass validation
        with pytest.raises(ConfigurationError, match="Unknown LLM backend"):
            get_llm_backend(cfg)

    @pytest.mark.asyncio
    async def test_ollama_probe_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = OllamaBackend(model="qwen2.5-vl:7b")

        async def fake_get(self: Any, url: str) -> Any:
            assert url.endswith("/api/tags")
            return MockResponse(200, {"models": [{"name": "qwen2.5-vl:7b"}]})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        await backend.probe()  # should not raise

    @pytest.mark.asyncio
    async def test_ollama_probe_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = OllamaBackend(model="qwen2.5-vl:7b")

        async def fake_get(self: Any, url: str) -> Any:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        with pytest.raises(AgentLLMError, match="Ollama unreachable"):
            await backend.probe()

    @pytest.mark.asyncio
    async def test_ollama_probe_model_not_pulled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = OllamaBackend(model="qwen2.5-vl:7b")

        async def fake_get(self: Any, url: str) -> Any:
            return MockResponse(200, {"models": [{"name": "llama3:7b"}]})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        with pytest.raises(AgentLLMError, match="not pulled"):
            await backend.probe()

    @pytest.mark.asyncio
    async def test_ollama_probe_accepts_base_tag_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Ollama lists ``qwen2.5-vl:latest`` but user wants
        # ``qwen2.5-vl:7b`` — base tags should match.
        backend = OllamaBackend(model="qwen2.5-vl:7b")

        async def fake_get(self: Any, url: str) -> Any:
            return MockResponse(200, {"models": [{"name": "qwen2.5-vl:latest"}]})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        await backend.probe()

    def test_to_browser_use_llm_lazy_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # browser-use ships its own LLM wrappers under browser_use.llm.*;
        # if browser-use isn't installed at all, ``to_browser_use_llm``
        # surfaces an ``AgentLLMError`` with a useful install hint.
        monkeypatch.setitem(sys.modules, "browser_use.llm.ollama.chat", None)
        backend = OllamaBackend(model="x")
        with pytest.raises(AgentLLMError, match="browser-use not installed"):
            backend.to_browser_use_llm()

    def test_to_crawl4ai_provider_returns_litellm_string(self) -> None:
        backend = OllamaBackend(model="qwen2.5-vl:7b", base_url="http://h:11434")
        provider, base, token = backend.to_crawl4ai_provider()
        assert provider == "ollama/qwen2.5-vl:7b"
        assert base == "http://h:11434"
        assert token is None


class TestOpenAICompatBackend:
    @pytest.mark.asyncio
    async def test_probe_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = OpenAICompatBackend(
            model="qwen3-coder:30b",
            base_url="http://localhost:8080",
            api_key="sk-local",
        )

        async def fake_get(self: Any, url: str, **_: Any) -> Any:
            assert url.endswith("/v1/models")
            return MockResponse(200, {"data": [{"id": "qwen3-coder:30b"}]})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        await backend.probe()

    @pytest.mark.asyncio
    async def test_probe_sends_auth_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = OpenAICompatBackend(
            model="gpt-4o", base_url="http://api.example.com", api_key="sk-secret"
        )
        captured: dict[str, Any] = {}

        class FakeClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def get(self, url: str, **_: Any) -> Any:
                return MockResponse(200, {"data": [{"id": "gpt-4o"}]})

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        await backend.probe()
        assert captured.get("headers", {}).get("Authorization") == "Bearer sk-secret"

    @pytest.mark.asyncio
    async def test_probe_model_not_in_list_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = OpenAICompatBackend(model="missing-model", base_url="http://x")

        async def fake_get(self: Any, url: str, **_: Any) -> Any:
            return MockResponse(200, {"data": [{"id": "other-model"}]})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        with pytest.raises(AgentLLMError, match="not listed"):
            await backend.probe()

    @pytest.mark.asyncio
    async def test_probe_empty_model_list_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = OpenAICompatBackend(model="any-model", base_url="http://x")

        async def fake_get(self: Any, url: str, **_: Any) -> Any:
            return MockResponse(200, {"data": []})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        await backend.probe()  # must not raise

    @pytest.mark.asyncio
    async def test_probe_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = OpenAICompatBackend(model="m", base_url="http://x")

        async def fake_get(self: Any, url: str, **_: Any) -> Any:
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        with pytest.raises(AgentLLMError, match="OpenAI-compat server unreachable"):
            await backend.probe()

    @pytest.mark.asyncio
    async def test_probe_http_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = OpenAICompatBackend(model="m", base_url="http://x")

        async def fake_get(self: Any, url: str, **_: Any) -> Any:
            return MockResponse(503, {"error": "down"})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        with pytest.raises(AgentLLMError, match="HTTP 503"):
            await backend.probe()

    def test_to_crawl4ai_provider(self) -> None:
        b = OpenAICompatBackend(model="m", base_url="http://h:1", api_key="key")
        provider, base, token = b.to_crawl4ai_provider()
        assert provider == "openai/m"
        assert base == "http://h:1/v1"
        assert token == "key"


class TestBehaviorHelpers:
    @pytest.mark.asyncio
    async def test_humanlike_pre_and_post_navigate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Don't actually sleep — assert the methods complete.
        async def instant(_: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", instant)
        policy = get_behavior_policy("humanlike")
        await policy.pre_navigate()
        await policy.post_navigate()

    def test_humanlike_mouse_path_returns_intermediate_points(self) -> None:
        policy = get_behavior_policy("humanlike")
        path = policy.mouse_path((0, 0), (200, 200))
        assert len(path) > 5
        # Path should be roughly between endpoints (with jitter).
        for x, y in path:
            assert -50 <= x <= 250
            assert -50 <= y <= 250

    async def test_behavior_consumer_humanlike_applies_shaping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from scrapper_tool.agent.backends.behavior import make_behavior_consumer

        async def instant(_: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", instant)
        policy = get_behavior_policy("humanlike")
        page = type("P", (), {"evaluate": AsyncMock()})()
        consumer = make_behavior_consumer(policy, full=True)
        await consumer(page, url="https://e.example")
        page.evaluate.assert_awaited()  # scroll shaping ran

    async def test_behavior_consumer_off_is_noop(self) -> None:
        from unittest.mock import AsyncMock

        from scrapper_tool.agent.backends.behavior import make_behavior_consumer

        policy = get_behavior_policy("off")
        page = type("P", (), {"evaluate": AsyncMock()})()
        consumer = make_behavior_consumer(policy, full=True)
        await consumer(page, url="https://e.example")
        page.evaluate.assert_not_awaited()  # off = no shaping

    async def test_behavior_consumer_e1_minimal_no_scroll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from scrapper_tool.agent.backends.behavior import make_behavior_consumer

        async def instant(_: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", instant)
        policy = get_behavior_policy("humanlike")
        page = type("P", (), {"evaluate": AsyncMock()})()
        consumer = make_behavior_consumer(policy, full=False)
        await consumer(page, url="https://e.example")
        page.evaluate.assert_not_awaited()  # E1 = settle only, no scroll


class TestVisionModelHeuristic:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("qwen2.5-vl:7b", True),
            ("qwen3-vl", True),
            ("llava:7b", True),
            ("minicpm-v:latest", True),
            ("vision-tower:13b", True),
            ("qwen3-coder:30b", False),
            ("llama3.3:70b", False),
            ("phi-4-mini:14b", False),
        ],
    )
    def test_detects_vision_models(self, model: str, expected: bool) -> None:
        assert is_vision_model(model) is expected

    @pytest.mark.parametrize("model", ["google/gemma-4-e4b", "pixtral-12b", "internvl3-8b"])
    def test_known_multimodal_families_are_recognised(self, model: str) -> None:
        """Families the original four tags missed entirely."""
        assert is_vision_model(model) is True


class TestSupportsVision:
    """The server, not the model name, is the authority on modality.

    The name heuristic answered False for both locally installed VLMs
    (``google/gemma-4-e4b``, ``qwen/qwen3.6-27b``) while LM Studio reported both
    as ``type=vlm``, so browse mode ran E2 blind. These pin the probe and, just as
    importantly, that every failure mode degrades to the old behaviour instead of
    raising into the agent loop.
    """

    @staticmethod
    def _serve(monkeypatch: pytest.MonkeyPatch, payload: Any, status: int = 200) -> list[str]:
        seen: list[str] = []

        class _Resp:
            status_code = status

            @staticmethod
            def json() -> Any:
                return payload

        class _Client:
            def __init__(self, **_: Any) -> None: ...
            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *_: object) -> None: ...
            async def get(self, url: str) -> _Resp:
                seen.append(url)
                return _Resp()

        monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _Client)
        return seen

    @pytest.mark.asyncio
    async def test_declared_vlm_wins_over_a_name_that_looks_textual(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact regression: a real VLM whose name carries no vision tag."""
        self._serve(monkeypatch, {"data": [{"id": "qwen/qwen3.6-27b", "type": "vlm"}]})
        assert is_vision_model("qwen/qwen3.6-27b") is False
        assert await supports_vision("qwen/qwen3.6-27b", "http://lm.test") is True

    @pytest.mark.asyncio
    async def test_declared_llm_wins_over_a_name_that_looks_visual(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Authority runs both ways — a 'vl' in the name must not override the server."""
        self._serve(monkeypatch, {"data": [{"id": "vlad-tuned-7b", "type": "llm"}]})
        assert is_vision_model("vlad-tuned-7b") is True
        assert await supports_vision("vlad-tuned-7b", "http://lm.test") is False

    @pytest.mark.asyncio
    async def test_queries_the_lm_studio_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._serve(monkeypatch, {"data": []})
        await supports_vision("m", "http://lm.test/")
        assert seen == ["http://lm.test/api/v0/models"]

    @pytest.mark.asyncio
    async def test_model_absent_from_catalogue_falls_back_to_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._serve(monkeypatch, {"data": [{"id": "other", "type": "llm"}]})
        assert await supports_vision("qwen2-vl-7b", "http://lm.test") is True
        assert await supports_vision("qwen3-coder", "http://lm.test") is False

    @pytest.mark.asyncio
    async def test_no_base_url_uses_name_heuristic(self) -> None:
        assert await supports_vision("qwen2-vl-7b", None) is True

    @pytest.mark.asyncio
    async def test_endpoint_absent_falls_back_to_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plain llama.cpp / vLLM / Ollama have no /api/v0/models — a 404, not an error."""
        self._serve(monkeypatch, {"data": []}, status=404)
        assert await supports_vision("qwen2-vl-7b", "http://llamacpp.test") is True

    @pytest.mark.asyncio
    async def test_unreachable_server_falls_back_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Client:
            def __init__(self, **_: Any) -> None: ...
            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *_: object) -> None: ...
            async def get(self, url: str) -> Any:
                raise llm_mod.httpx.ConnectError("refused")

        monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _Client)
        assert await supports_vision("qwen2-vl-7b", "http://down.test") is True

    @pytest.mark.asyncio
    async def test_garbage_payload_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._serve(monkeypatch, {"data": ["not-a-dict", {"no_id": 1}]})
        assert await supports_vision("qwen2-vl-7b", "http://lm.test") is True


# --- Captcha cascade ------------------------------------------------------


class TestCaptchaResolver:
    def test_no_solver_when_solver_is_none(self) -> None:
        cfg = AgentConfig(captcha_solver="none")
        from scrapper_tool.agent.backends.captcha import NoSolver

        assert isinstance(get_captcha_solver(cfg), NoSolver)

    def test_auto_without_key_yields_free_tiers_only(self) -> None:
        cfg = AgentConfig(captcha_solver="auto", captcha_api_key=None)
        from scrapper_tool.agent.backends.captcha import (
            AutoCascadeSolver,
            CamoufoxAutoSolver,
            TheykaSolver,
        )

        solver = get_captcha_solver(cfg)
        assert isinstance(solver, AutoCascadeSolver)
        # Inspect the private tier list — covered by class invariants.
        tiers = solver._tiers  # type: ignore[attr-defined]
        assert any(isinstance(t, CamoufoxAutoSolver) for t in tiers)
        assert any(isinstance(t, TheykaSolver) for t in tiers)
        # No paid tier without an api key.
        for t in tiers:
            assert not getattr(t, "requires_api_key", False)

    def test_auto_with_key_appends_paid_fallback(self) -> None:
        from pydantic import SecretStr

        from scrapper_tool.agent.backends.captcha import (
            AutoCascadeSolver,
            CapSolverSolver,
        )

        cfg = AgentConfig(
            captcha_solver="auto",
            captcha_api_key=SecretStr("sk_test"),
            captcha_paid_fallback="capsolver",
        )
        solver = get_captcha_solver(cfg)
        assert isinstance(solver, AutoCascadeSolver)
        tiers = solver._tiers  # type: ignore[attr-defined]
        assert any(isinstance(t, CapSolverSolver) for t in tiers)

    def test_explicit_paid_solver_without_key_falls_back_to_no_solver(self) -> None:
        from scrapper_tool.agent.backends.captcha import NoSolver

        cfg = AgentConfig(captcha_solver="capsolver", captcha_api_key=None)
        assert isinstance(get_captcha_solver(cfg), NoSolver)

    def test_unknown_solver_raises(self) -> None:
        cfg = AgentConfig.model_construct(captcha_solver="bogus", captcha_api_key=None)
        with pytest.raises(ConfigurationError, match="Unknown captcha solver"):
            get_captcha_solver(cfg)

    @pytest.mark.parametrize(
        ("fallback", "cls_name"),
        [("nopecha", "NopechaSolver"), ("twocaptcha", "TwoCaptchaSolver")],
    )
    def test_auto_with_key_other_paid_fallbacks(self, fallback: str, cls_name: str) -> None:
        from pydantic import SecretStr

        import scrapper_tool.agent.backends.captcha as captcha_mod

        cfg = AgentConfig(
            captcha_solver="auto",
            captcha_api_key=SecretStr("sk_test"),
            captcha_paid_fallback=fallback,  # type: ignore[arg-type]
        )
        solver = get_captcha_solver(cfg)
        tiers = solver._tiers  # type: ignore[attr-defined]
        assert any(isinstance(t, getattr(captcha_mod, cls_name)) for t in tiers)

    @pytest.mark.parametrize(
        ("name", "cls_name"),
        [
            ("camoufox-auto", "CamoufoxAutoSolver"),
            ("theyka", "TheykaSolver"),
        ],
    )
    def test_single_name_free_solvers(self, name: str, cls_name: str) -> None:
        import scrapper_tool.agent.backends.captcha as captcha_mod

        cfg = AgentConfig(captcha_solver=name)  # type: ignore[arg-type]
        solver = get_captcha_solver(cfg)
        assert isinstance(solver, getattr(captcha_mod, cls_name))

    @pytest.mark.parametrize("name", ["nopecha", "twocaptcha"])
    def test_single_name_paid_solvers_with_key(self, name: str) -> None:
        from pydantic import SecretStr

        cfg = AgentConfig(captcha_solver=name, captcha_api_key=SecretStr("k"))  # type: ignore[arg-type]
        solver = get_captcha_solver(cfg)
        assert solver.name == name

    async def test_theyka_solver_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Cover the TheykaSolver happy path (a lazy-imported turnstile_solver).
        from unittest.mock import AsyncMock

        from scrapper_tool.agent.backends.captcha import TheykaSolver

        fake_mod = types.ModuleType("turnstile_solver")

        class _Solver:
            async def solve(self, *, url: str, sitekey: str) -> str:
                return "theyka-token"

        fake_mod.Solver = _Solver  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "turnstile_solver", fake_mod)

        _ = AsyncMock  # keep import parity with sibling tests
        token = await TheykaSolver().solve("turnstile", "0xKEY", "https://e.example")
        assert token == "theyka-token"


# --- Mocks ---------------------------------------------------------------


class MockResponse:
    """httpx.Response stand-in — supports the surface our backends touch."""

    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", "http://test")
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=req,
                response=httpx.Response(self.status_code, request=req),
            )


# Silence unused-imports — referenced via class inspection in TestCaptchaResolver.
_ = (AsyncMock, MagicMock)


class TestDependencyCoexistence:
    """Pins that the browser-use 0.13 upgrade forced, asserted so a future
    upgrade that changes them is a visible decision rather than a surprise.

    browser-use 0.13 pins pydantic and (transitively) playwright exactly, and in
    the [full] environment those win over scrapling[fetchers]'s newer exact pins.
    These are deliberately accepted, not overridden: the pins guard browser-use's
    LLM-tool-call validation and CDP attach, the two paths where a silent minor
    bump has historically broken it. If these assertions fail after an upgrade,
    re-read whether the coexistence still holds before loosening them.
    """

    def test_pydantic_matches_browser_uses_pin(self) -> None:
        pytest.importorskip("browser_use")
        import importlib.metadata as md

        # browser-use 0.13.x pins pydantic==2.12.5 exactly. We honour it rather
        # than override, so the resolved version must be in the 2.12 line.
        assert md.version("pydantic").startswith("2.12."), (
            "pydantic moved off browser-use's pin — confirm E2 tool-calls still "
            "validate before accepting the new version"
        )

    def test_typing_extensions_override_holds(self) -> None:
        """We DO override browser-use's exact typing-extensions pin.

        typing-extensions is additive by contract, so pinning it back would drag
        scrapling and patchright backwards for no benefit. This asserts the
        override in pyproject's [tool.uv] actually took effect.
        """
        import importlib.metadata as md

        major, minor, *_ = md.version("typing-extensions").split(".")
        assert (int(major), int(minor)) >= (4, 16), (
            "the typing-extensions override was lost; scrapling/patchright will "
            "have regressed with it"
        )


class TestCaptchaTaskPayloads:
    """Every declared kind must build a payload the provider will actually accept.

    The sitekey does not live under one field name across task types, and three
    types take no sitekey at all. The old code sent ``websiteKey`` for
    everything, so FunCaptcha, DataDome, AWS WAF and image tasks would have been
    rejected by the API even when the kind was right. Nobody noticed because
    detection could not produce those kinds in the first place — fixing detection
    without this would just move the failure one layer down.
    """

    @staticmethod
    def _payload(kind: str, **kw: Any) -> dict[str, Any]:
        from scrapper_tool.agent.backends.captcha import _capsolver_task_payload

        return _capsolver_task_payload(kind, kw.pop("site_key", "KEY"), "https://x.example", **kw)

    def test_sitekey_kinds_use_website_key(self) -> None:
        for kind in ("turnstile", "hcaptcha", "recaptcha-v2", "recaptcha-v3"):
            assert self._payload(kind)["websiteKey"] == "KEY", kind

    def test_funcaptcha_uses_website_public_key(self) -> None:
        payload = self._payload("funcaptcha", extra={"surl": "https://client-api.arkoselabs.com"})
        assert payload["websitePublicKey"] == "KEY"
        assert "websiteKey" not in payload
        assert payload["funcaptchaApiJSSubdomain"] == "https://client-api.arkoselabs.com"

    def test_arkose_maps_to_the_same_funcaptcha_task(self) -> None:
        assert self._payload("arkose")["type"] == self._payload("funcaptcha")["type"]

    def test_datadome_sends_the_challenge_url_not_a_sitekey(self) -> None:
        payload = self._payload(
            "datadome", site_key="", extra={"captchaUrl": "https://geo.captcha-delivery.com/c?x=1"}
        )
        assert payload["captchaUrl"] == "https://geo.captcha-delivery.com/c?x=1"
        assert "websiteKey" not in payload

    def test_aws_waf_forwards_the_goku_props(self) -> None:
        payload = self._payload(
            "aws-waf",
            site_key="",
            extra={"awsKey": "K", "awsIv": "I", "awsContext": "C", "awsChallengeJS": "https://j"},
        )
        assert (payload["awsKey"], payload["awsIv"], payload["awsContext"]) == ("K", "I", "C")
        assert "websiteKey" not in payload

    def test_image_sends_bytes_and_drops_the_url(self) -> None:
        """ImageToTextTask is solved from pixels; websiteURL is meaningless to it."""
        payload = self._payload("image", site_key="", extra={"body": "BASE64", "image_url": "u"})
        assert payload["body"] == "BASE64"
        assert "websiteURL" not in payload
        assert "image_url" not in payload

    def test_geetest_sends_gt_and_challenge(self) -> None:
        payload = self._payload("geetest", extra={"challenge": "NONCE", "version": "4"})
        assert payload["gt"] == "KEY"
        assert payload["challenge"] == "NONCE"
        # `version` is ours for bookkeeping, not a CapSolver field.
        assert "version" not in payload

    def test_recaptcha_v3_page_action(self) -> None:
        assert self._payload("recaptcha-v3", action="login")["pageAction"] == "login"

    def test_every_declared_kind_builds_a_payload(self) -> None:
        """No CaptchaKind may be un-routable — that was the whole bug."""
        from typing import get_args

        from scrapper_tool.agent.backends.captcha import CaptchaKind

        for kind in get_args(CaptchaKind):
            payload = self._payload(kind)
            assert payload["type"], kind

    def test_unconsumed_extras_still_pass_through(self) -> None:
        assert self._payload("turnstile", extra={"proxy": "http://p"})["proxy"] == "http://p"


class TestTwoCaptchaParams:
    """2Captcha has the same per-method key-naming trap as CapSolver."""

    @staticmethod
    async def _params(monkeypatch: pytest.MonkeyPatch, kind: str, **kw: Any) -> dict[str, str]:
        """Capture the query params 2Captcha's ``in.php`` submit would receive.

        The stub answers ``status: 0``, so ``solve`` raises right after the
        submit — which is all we need, and keeps the test off the network.
        """
        from scrapper_tool.agent.backends import captcha as cap

        captured: dict[str, str] = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def raise_for_status() -> None: ...

            @staticmethod
            def json() -> dict[str, Any]:
                return {"status": 0, "request": "ERROR_STOP"}

        class _Client:
            def __init__(self, **_: Any) -> None: ...

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *_: object) -> None: ...

            async def get(self, url: str, params: dict[str, str], timeout: float) -> _Resp:
                captured.update(params)
                return _Resp()

        monkeypatch.setattr(cap.httpx, "AsyncClient", _Client)
        with pytest.raises(cap.CaptchaSolveError):
            await cap.TwoCaptchaSolver(api_key="k").solve(
                kind, kw.pop("site_key", "KEY"), "https://x.example", **kw
            )
        return captured

    @pytest.mark.asyncio
    async def test_funcaptcha_uses_publickey(self, monkeypatch: pytest.MonkeyPatch) -> None:
        params = await self._params(
            monkeypatch, "funcaptcha", extra={"surl": "https://arkose.test"}
        )
        assert params["publickey"] == "KEY"
        assert "sitekey" not in params
        assert params["surl"] == "https://arkose.test"

    @pytest.mark.asyncio
    async def test_geetest_uses_gt_and_challenge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        params = await self._params(monkeypatch, "geetest", extra={"challenge": "NONCE"})
        assert params["gt"] == "KEY"
        assert params["challenge"] == "NONCE"
        assert "sitekey" not in params

    @pytest.mark.asyncio
    async def test_image_sends_body_and_drops_pageurl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        params = await self._params(monkeypatch, "image", site_key="", extra={"body": "B64"})
        assert params["body"] == "B64"
        assert "pageurl" not in params

    @pytest.mark.asyncio
    async def test_ordinary_kinds_keep_sitekey(self, monkeypatch: pytest.MonkeyPatch) -> None:
        params = await self._params(monkeypatch, "hcaptcha")
        assert params["sitekey"] == "KEY"


class TestAutoCascadeCoverage:
    def test_supported_reflects_the_configured_tiers(self) -> None:
        """Was hard-coded to the four free kinds, under-reporting a paid cascade.

        A caller checking ``supported`` before dispatching would conclude the
        cascade could not handle DataDome when the CapSolver tier inside it can.
        """
        from pydantic import SecretStr

        from scrapper_tool.agent.backends.captcha import AutoCascadeSolver

        free = get_captcha_solver(AgentConfig(captcha_solver="auto", captcha_api_key=None))
        assert isinstance(free, AutoCascadeSolver)
        assert free.supported == frozenset({"turnstile"})

        paid = get_captcha_solver(
            AgentConfig(
                captcha_solver="auto",
                captcha_api_key=SecretStr("sk"),
                captcha_paid_fallback="capsolver",
            )
        )
        assert {"datadome", "aws-waf", "funcaptcha", "geetest"} <= paid.supported


class TestCompleteVision:
    """The direct image-inference path the local captcha solvers need.

    Everything else on ``LLMBackend`` hands the model to another framework
    (browser-use, Crawl4AI); nothing could simply ask a question about an image,
    which is exactly what a grid or OCR solver does.
    """

    @staticmethod
    def _capture(
        monkeypatch: pytest.MonkeyPatch, payload: Any, status: int = 200
    ) -> dict[str, Any]:
        sent: dict[str, Any] = {}

        class _Resp:
            status_code = status
            text = "err"

            @staticmethod
            def json() -> Any:
                return payload

        class _Client:
            def __init__(self, **kw: Any) -> None:
                sent["headers"] = kw.get("headers")

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *_: object) -> None: ...

            async def post(self, url: str, json: dict[str, Any]) -> _Resp:
                sent["url"] = url
                sent["json"] = json
                return _Resp()

        monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _Client)
        return sent

    @pytest.mark.asyncio
    async def test_openai_compat_sends_multimodal_content_parts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._capture(monkeypatch, {"choices": [{"message": {"content": "Red"}}]})
        backend = OpenAICompatBackend(model="m", base_url="http://lm.test")
        assert await backend.complete_vision("what colour?", ["AAA", "BBB"]) == "Red"

        assert sent["url"] == "http://lm.test/v1/chat/completions"
        content = sent["json"]["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "what colour?"}
        # Both images must survive: a grid solve sends the challenge plus tiles.
        urls = [p["image_url"]["url"] for p in content[1:]]
        assert urls == ["data:image/png;base64,AAA", "data:image/png;base64,BBB"]

    @pytest.mark.asyncio
    async def test_ollama_sends_native_images_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ollama's /api/chat takes images as a sibling list, not content parts."""
        sent = self._capture(monkeypatch, {"message": {"content": "Blue"}})
        backend = OllamaBackend(model="m", base_url="http://olla.test")
        assert await backend.complete_vision("q", ["AAA"]) == "Blue"
        assert sent["url"] == "http://olla.test/api/chat"
        assert sent["json"]["messages"][0]["images"] == ["AAA"]
        assert sent["json"]["stream"] is False

    @pytest.mark.asyncio
    async def test_api_key_becomes_a_bearer_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._capture(monkeypatch, {"choices": [{"message": {"content": "x"}}]})
        backend = OpenAICompatBackend(model="m", base_url="http://lm.test", api_key="sk")
        await backend.complete_vision("q", ["A"])
        assert sent["headers"] == {"Authorization": "Bearer sk"}

    @pytest.mark.asyncio
    async def test_reasoning_only_reply_is_surfaced_not_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The measured starvation trap.

        ``google/gemma-4-e4b`` spent an entire 20-token budget on
        ``reasoning_content`` and returned ``content: ""``. Returning "" would
        make a solver read starvation as a solve failure, so the partial
        reasoning is surfaced (with a warning) instead.
        """
        self._capture(
            monkeypatch,
            {
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "The image looks red"},
                        "finish_reason": "length",
                    }
                ]
            },
        )
        backend = OpenAICompatBackend(model="m", base_url="http://lm.test")
        assert "red" in (await backend.complete_vision("q", ["A"])).lower()

    @pytest.mark.asyncio
    async def test_content_returned_as_parts_is_joined(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._capture(
            monkeypatch,
            {"choices": [{"message": {"content": [{"text": "ti"}, {"text": "les"}]}}]},
        )
        backend = OpenAICompatBackend(model="m", base_url="http://lm.test")
        assert await backend.complete_vision("q", ["A"]) == "tiles"

    @pytest.mark.asyncio
    async def test_empty_reply_raises_with_a_pointer_to_the_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._capture(monkeypatch, {"choices": [{"message": {"content": ""}}]})
        backend = OpenAICompatBackend(model="m", base_url="http://lm.test")
        with pytest.raises(AgentLLMError, match="max_tokens"):
            await backend.complete_vision("q", ["A"])

    @pytest.mark.asyncio
    async def test_http_error_raises_agent_llm_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._capture(monkeypatch, {}, status=500)
        backend = OpenAICompatBackend(model="m", base_url="http://lm.test")
        with pytest.raises(AgentLLMError, match="500"):
            await backend.complete_vision("q", ["A"])

    @pytest.mark.asyncio
    async def test_transport_failure_raises_agent_llm_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Client:
            def __init__(self, **_: Any) -> None: ...

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *_: object) -> None: ...

            async def post(self, url: str, json: dict[str, Any]) -> Any:
                raise llm_mod.httpx.ConnectError("refused")

        monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _Client)
        backend = OpenAICompatBackend(model="m", base_url="http://down.test")
        with pytest.raises(AgentLLMError, match="Vision call failed"):
            await backend.complete_vision("q", ["A"])


class TestVisionBackendResolution:
    """Extraction and captcha-solving want opposite things from a model.

    A downstream integration benchmarked `google/gemma-4-e4b` as the BEST model
    for E1 extraction — 1.1 s, 3-of-3 fields, beating every larger candidate,
    because extraction is instruction-following. That same model scores **0/5**
    on live reCAPTCHA grids, which are spatial vision. Pinning one model for both
    silently breaks whichever job it was not chosen for, so the captcha tier can
    resolve its own.
    """

    @staticmethod
    def _serve(monkeypatch: pytest.MonkeyPatch, vlm_models: set[str]) -> None:
        class _Resp:
            status_code = 200

            @staticmethod
            def json() -> Any:
                return {
                    "data": [
                        {"id": m, "type": "vlm" if m in vlm_models else "llm"}
                        for m in ("google/gemma-4-e4b", "qwen/qwen3.8-27b")
                    ]
                }

        class _Client:
            def __init__(self, **_: Any) -> None: ...

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *_: object) -> None: ...

            async def get(self, url: str) -> _Resp:
                return _Resp()

        monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _Client)

    @pytest.mark.asyncio
    async def test_captcha_model_overrides_the_extraction_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The integration case: a small model for extraction, a 27B for grids."""
        self._serve(monkeypatch, {"qwen/qwen3.8-27b"})
        cfg = AgentConfig(
            llm="openai_compat",
            model="google/gemma-4-e4b",
            captcha_vision_model="qwen/qwen3.8-27b",
            ollama_url="http://lm.test",
        )
        backend = await llm_mod.get_vision_backend(cfg)
        assert backend is not None
        assert backend.model == "qwen/qwen3.8-27b"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_main_model_when_set_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``None`` is how an operator says "one model is fine for both here".

        Stated explicitly rather than by omission: since v2.2.2 the field has a
        non-``None`` default, so leaving it out of the constructor no longer
        means "reuse the main model" — it means "use the 27B default". The
        fallback itself is unchanged and still keyed on ``None``.
        """
        self._serve(monkeypatch, {"google/gemma-4-e4b"})
        cfg = AgentConfig(
            llm="openai_compat",
            model="google/gemma-4-e4b",
            captcha_vision_model=None,
            ollama_url="http://lm.test",
        )
        backend = await llm_mod.get_vision_backend(cfg)
        assert backend is not None
        assert backend.model == "google/gemma-4-e4b"

    @pytest.mark.asyncio
    async def test_none_when_the_resolved_model_cannot_see(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skip the grid tier rather than hand a text-only model an image."""
        self._serve(monkeypatch, set())
        cfg = AgentConfig(
            llm="openai_compat",
            model="google/gemma-4-e4b",
            captcha_vision_model=None,
            ollama_url="http://lm.test",
        )
        assert await llm_mod.get_vision_backend(cfg) is None
