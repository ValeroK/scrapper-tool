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
