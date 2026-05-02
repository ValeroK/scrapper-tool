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
    BotasaurusBackend,
    CamoufoxBackend,
    PatchrightBackend,
    ScraplingBackend,
    ZendriverBackend,
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
from scrapper_tool.errors import AgentLLMError

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
            "zendriver": ZendriverBackend,
            "scrapling": ScraplingBackend,
            "botasaurus": BotasaurusBackend,
        }
        for name, cls in cases.items():
            assert isinstance(get_browser_backend(name), cls)

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown browser backend"):
            get_browser_backend("internet-explorer")

    def test_camoufox_install_error_message_is_helpful(self) -> None:
        # We can't drive `.launch()` in unit tests (real Camoufox needed),
        # so verify the error message at the module level.
        from scrapper_tool.agent.backends import browser as browser_mod

        assert "[llm-agent]" in browser_mod._CAMOUFOX_NOT_INSTALLED
        assert "camoufox fetch" in browser_mod._CAMOUFOX_NOT_INSTALLED
        assert "[llm-agent]" in browser_mod._PATCHRIGHT_NOT_INSTALLED
        assert "[zendriver-backend]" in browser_mod._ZENDRIVER_NOT_INSTALLED
        assert "[botasaurus-backend]" in browser_mod._BOTASAURUS_NOT_INSTALLED
        assert "[hostile]" in browser_mod._SCRAPLING_NOT_INSTALLED


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
        with pytest.raises(ValueError, match="Unknown fingerprint"):
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
        with pytest.raises(ValueError, match="Unknown behavior policy"):
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

    def test_unknown_raises(self) -> None:
        cfg = AgentConfig.model_construct(llm="evil")  # bypass validation
        with pytest.raises(ValueError, match="Unknown LLM backend"):
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

        async def fake_get(self: Any, url: str) -> Any:
            assert url.endswith("/v1/models")
            return MockResponse(200, {"data": [{"id": "qwen3-coder:30b"}]})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        await backend.probe()

    @pytest.mark.asyncio
    async def test_probe_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = OpenAICompatBackend(model="m", base_url="http://x")

        async def fake_get(self: Any, url: str) -> Any:
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        with pytest.raises(AgentLLMError, match="OpenAI-compat server unreachable"):
            await backend.probe()

    @pytest.mark.asyncio
    async def test_probe_http_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = OpenAICompatBackend(model="m", base_url="http://x")

        async def fake_get(self: Any, url: str) -> Any:
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
        with pytest.raises(ValueError, match="Unknown captcha solver"):
            get_captcha_solver(cfg)


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
