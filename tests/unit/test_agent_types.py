"""Unit tests for ``scrapper_tool.agent.types``.

Pure data layer — exercise:

- Default values match the "ultimate scraper" stack (Camoufox + Ollama
  + qwen3-vl:8b + auto captcha cascade + humanlike behavior).
- ``AgentConfig.from_env`` correctly reads every documented env var.
- ``AgentConfig.merged`` produces independent copies and skips Nones.
- ``AgentResult`` round-trips through pydantic JSON.
"""

from __future__ import annotations

import pytest

from scrapper_tool.agent.types import (
    DEFAULT_CAPTCHA_VISION_MODEL,
    ActionTrace,
    AgentConfig,
    AgentResult,
)


class TestAgentConfigDefaults:
    def test_defaults_are_ultimate_scraper(self) -> None:
        cfg = AgentConfig()
        assert cfg.browser == "camoufox"
        assert cfg.model == "qwen3-vl:8b"
        assert cfg.llm == "ollama"
        assert cfg.behavior == "humanlike"
        assert cfg.captcha_solver == "auto"
        assert cfg.fingerprint == "browserforge"
        assert cfg.respect_robots is True
        assert cfg.captcha_api_key is None


class TestFromEnv:
    def test_from_env_with_no_vars_uses_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in list(__import__("os").environ.keys()):
            if k.startswith("SCRAPPER_TOOL_"):
                monkeypatch.delenv(k, raising=False)
        cfg = AgentConfig.from_env()
        assert cfg.browser == "camoufox"
        assert cfg.model == "qwen3-vl:8b"

    def test_from_env_reads_all_documented_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_BROWSER", "patchright")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_FINGERPRINT", "browserforge")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_BEHAVIOR", "fast")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_HEADFUL", "1")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_PROXY", "http://proxy:8080")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_LLM", "ollama")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_MODEL", "qwen3-vl:8b")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_OLLAMA_URL", "http://10.0.0.5:11434")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_MAX_STEPS", "30")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_TIMEOUT_S", "240")
        monkeypatch.setenv("SCRAPPER_TOOL_CAPTCHA_SOLVER", "capsolver")
        monkeypatch.setenv("SCRAPPER_TOOL_CAPTCHA_KEY", "sk_test_123")
        monkeypatch.setenv("SCRAPPER_TOOL_CAPTCHA_PAID_FALLBACK", "twocaptcha")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_RESPECT_ROBOTS", "false")

        cfg = AgentConfig.from_env()
        assert cfg.browser == "patchright"
        assert cfg.behavior == "fast"
        assert cfg.headful is True
        assert cfg.proxy == "http://proxy:8080"
        assert cfg.model == "qwen3-vl:8b"
        assert cfg.ollama_url == "http://10.0.0.5:11434"
        assert cfg.max_steps == 30
        assert cfg.timeout_s == 240.0
        assert cfg.captcha_solver == "capsolver"
        assert cfg.captcha_api_key is not None
        assert cfg.captcha_api_key.get_secret_value() == "sk_test_123"
        assert cfg.captcha_paid_fallback == "twocaptcha"
        assert cfg.respect_robots is False

    def test_from_env_reads_render_knobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """v1.6.0 Camoufox render/stealth knobs round-trip through from_env."""
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_CAMOUFOX_DISPLAY", "virtual")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_BLOCK_IMAGES", "1")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_FINGERPRINT_PRESET", "true")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_CAMOUFOX_OS", "windows")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_CAMOUFOX_LOCALE", "he-IL")
        cfg = AgentConfig.from_env()
        assert cfg.camoufox_headless_mode == "virtual"
        assert cfg.block_images is True
        assert cfg.fingerprint_preset is True
        assert cfg.camoufox_os == "windows"
        assert cfg.camoufox_locale == "he-IL"

    def test_render_knobs_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in list(__import__("os").environ.keys()):
            if k.startswith("SCRAPPER_TOOL_"):
                monkeypatch.delenv(k, raising=False)
        cfg = AgentConfig.from_env()
        assert cfg.camoufox_headless_mode == "headless"
        assert cfg.block_images is False
        assert cfg.fingerprint_preset is False

    def test_from_env_reads_llm_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_LLM", "openai_compat")
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_LLM_API_KEY", "sk-openai-test-key")
        cfg = AgentConfig.from_env()
        assert cfg.llm == "openai_compat"
        assert cfg.llm_api_key is not None
        assert cfg.llm_api_key.get_secret_value() == "sk-openai-test-key"

    def test_envbool_handles_truthy_strings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for truthy in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("SCRAPPER_TOOL_AGENT_HEADFUL", truthy)
            assert AgentConfig.from_env().headful is True
        for falsy in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("SCRAPPER_TOOL_AGENT_HEADFUL", falsy)
            assert AgentConfig.from_env().headful is False


class TestMerged:
    def test_merged_returns_independent_copy(self) -> None:
        a = AgentConfig()
        b = a.merged(model="other:9b")
        assert a.model == "qwen3-vl:8b"
        assert b.model == "other:9b"

    def test_merged_skips_none_overrides(self) -> None:
        a = AgentConfig(model="foo:7b")
        b = a.merged(model=None)
        assert b.model == "foo:7b"

    def test_merged_with_no_overrides_is_a_noop(self) -> None:
        a = AgentConfig(model="foo:7b")
        b = a.merged()
        assert b.model == a.model


class TestAgentResultSerialization:
    def test_round_trip_through_json(self) -> None:
        r = AgentResult(
            mode="extract",
            data={"title": "Hello"},
            final_url="https://example.com",
            actions=[
                ActionTrace(
                    step=1,
                    action="extract",
                    target="main h1",
                    screenshot_idx=None,
                    dom_snippet="<h1>Hello</h1>",
                    latency_ms=512,
                )
            ],
            tokens_used=128,
            blocked=False,
            error=None,
            duration_s=1.23,
            steps_used=1,
        )
        as_json = r.model_dump_json()
        roundtripped = AgentResult.model_validate_json(as_json)
        assert roundtripped.data == {"title": "Hello"}
        assert roundtripped.actions[0].action == "extract"
        assert roundtripped.steps_used == 1


class TestCaptchaVisionModel:
    """The grid tier gets its own model, and the two must not collapse into one.

    Extraction wants a small instruction-follower; grids want a large VLM. The
    repo's own measurements have them at opposite ends (gemma-4-e4b: 0/5 on
    grids but fastest at extraction), so a change that quietly makes one field
    follow the other breaks whichever job it was not chosen for -- silently,
    which is the failure mode these tests exist to prevent.
    """

    def test_vision_model_defaults_to_a_dedicated_model(self) -> None:
        cfg = AgentConfig()
        assert cfg.captcha_vision_model == DEFAULT_CAPTCHA_VISION_MODEL

    def test_vision_model_is_not_the_extraction_model(self) -> None:
        cfg = AgentConfig()
        assert cfg.captcha_vision_model != cfg.model, (
            "the whole point of the field is that these differ; if a change "
            "makes them equal, the grid tier silently inherits a model measured "
            "at 0-1/5 on reCAPTCHA"
        )

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_CAPTCHA_VISION_MODEL", "some-other-vlm:9b")
        assert AgentConfig.from_env().captcha_vision_model == "some-other-vlm:9b"

    def test_empty_env_value_means_reuse_the_extraction_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty and unset must not mean the same thing.

        Unset takes the default; explicitly empty is how an operator says "one
        model is fine for both here". Collapsing them would remove the only way
        to opt out of a second model download.
        """
        monkeypatch.setenv("SCRAPPER_TOOL_CAPTCHA_VISION_MODEL", "")
        assert AgentConfig.from_env().captcha_vision_model is None

    def test_unset_env_takes_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCRAPPER_TOOL_CAPTCHA_VISION_MODEL", raising=False)
        assert AgentConfig.from_env().captcha_vision_model == DEFAULT_CAPTCHA_VISION_MODEL

    def test_explicit_none_is_preserved(self) -> None:
        assert AgentConfig(captcha_vision_model=None).captcha_vision_model is None
