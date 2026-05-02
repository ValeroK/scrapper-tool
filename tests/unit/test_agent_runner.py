"""Unit tests for ``scrapper_tool.agent.runner``.

The runner is mostly a thin orchestrator over ``extract`` and
``browse`` modules. We patch those modules' run functions to capture
calls and exercise the public API surface (``agent_extract``,
``agent_browse``, ``agent_session``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from scrapper_tool import agent as agent_pkg
from scrapper_tool.agent import runner as runner_mod
from scrapper_tool.agent.types import AgentConfig, AgentResult


@pytest.fixture
def patch_run_extract(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    fake = AsyncMock(
        return_value=AgentResult(
            mode="extract",
            data={"ok": True},
            final_url="https://e.com",
            steps_used=1,
        )
    )
    monkeypatch.setattr(runner_mod._extract, "run_extract", fake)
    return fake


@pytest.fixture
def patch_run_browse(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    fake = AsyncMock(
        return_value=AgentResult(
            mode="browse",
            data={"ok": True},
            final_url="https://e.com",
            steps_used=3,
        )
    )
    monkeypatch.setattr(runner_mod._browse, "run_browse", fake)
    return fake


class TestAgentExtract:
    @pytest.mark.asyncio
    async def test_uses_default_config_when_none_passed(
        self, patch_run_extract: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Make from_env deterministic.
        monkeypatch.setattr(AgentConfig, "from_env", classmethod(lambda cls: cls()))

        result = await runner_mod.agent_extract("https://example.com", schema={"x": "int"})
        assert result.data == {"ok": True}
        patch_run_extract.assert_awaited_once()
        kwargs = patch_run_extract.await_args.kwargs
        assert kwargs["instruction"] is None
        cfg = kwargs["config"]
        assert isinstance(cfg, AgentConfig)
        assert cfg.browser == "camoufox"  # default

    @pytest.mark.asyncio
    async def test_overrides_apply(
        self, patch_run_extract: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(AgentConfig, "from_env", classmethod(lambda cls: cls()))
        await runner_mod.agent_extract(
            "https://example.com",
            schema={"x": "int"},
            model="qwen3-vl:8b",
            browser="patchright",
        )
        cfg = patch_run_extract.await_args.kwargs["config"]
        assert cfg.model == "qwen3-vl:8b"
        assert cfg.browser == "patchright"

    @pytest.mark.asyncio
    async def test_explicit_config_used_verbatim(self, patch_run_extract: AsyncMock) -> None:
        cfg = AgentConfig(model="custom:1b", browser="zendriver")
        await runner_mod.agent_extract("https://e.com", schema={"x": 1}, config=cfg)
        passed = patch_run_extract.await_args.kwargs["config"]
        assert passed is cfg


class TestAgentBrowse:
    @pytest.mark.asyncio
    async def test_passes_schema_through(
        self, patch_run_browse: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(AgentConfig, "from_env", classmethod(lambda cls: cls()))
        schema = {"type": "object"}
        await runner_mod.agent_browse("https://e.com", "do the thing", schema=schema)
        kwargs = patch_run_browse.await_args.kwargs
        assert kwargs["schema"] == schema


class TestAgentSession:
    @pytest.mark.asyncio
    async def test_session_extract_and_browse(
        self,
        patch_run_extract: AsyncMock,
        patch_run_browse: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Mock the LLM probe so session entry succeeds without Ollama.
        from scrapper_tool.agent.backends.llm import OllamaBackend

        monkeypatch.setattr(OllamaBackend, "probe", AsyncMock(return_value=None))
        monkeypatch.setattr(AgentConfig, "from_env", classmethod(lambda cls: cls()))

        async with runner_mod.agent_session() as s:
            r1 = await s.extract("https://a.com", schema={"x": 1})
            r2 = await s.browse("https://b.com", "log in")

        assert r1.mode == "extract"
        assert r2.mode == "browse"
        assert patch_run_extract.await_count == 1
        assert patch_run_browse.await_count == 1

    @pytest.mark.asyncio
    async def test_session_probes_llm_at_entry(
        self,
        patch_run_extract: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        probe = AsyncMock(return_value=None)
        from scrapper_tool.agent.backends.llm import OllamaBackend

        monkeypatch.setattr(OllamaBackend, "probe", probe)
        monkeypatch.setattr(AgentConfig, "from_env", classmethod(lambda cls: cls()))

        async with runner_mod.agent_session():
            pass
        assert probe.await_count == 1


class TestPublicReExports:
    def test_top_level_lazy_attr_access(self) -> None:
        import scrapper_tool

        # PEP 562 __getattr__ should resolve.
        assert callable(scrapper_tool.agent_extract)
        assert callable(scrapper_tool.agent_browse)

    def test_unknown_attr_still_raises(self) -> None:
        import scrapper_tool

        with pytest.raises(AttributeError):
            _ = scrapper_tool.does_not_exist  # type: ignore[attr-defined]

    def test_pattern_e_module_re_exports(self) -> None:
        from scrapper_tool.patterns import e

        assert e.agent_extract is agent_pkg.agent_extract
        assert e.agent_browse is agent_pkg.agent_browse


_ = Any  # silence
