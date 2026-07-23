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
)
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
