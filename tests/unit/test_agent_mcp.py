"""Unit tests for the new MCP tools (``agent_extract`` and ``agent_browse``).

Patches the underlying agent runner so the test exercises the MCP-side
serialization (truncation, screenshot capping, error envelope) without
touching real Crawl4AI / browser-use.

Skipped when the ``[agent]`` extra is not installed (no FastMCP).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip(
    "mcp.server.fastmcp",
    reason="MCP tests require the [agent] extra (pip install scrapper-tool[agent]).",
)

from scrapper_tool import mcp as mcp_module
from scrapper_tool.agent.types import AgentResult
from scrapper_tool.errors import AgentBlockedError, AgentError


@pytest.fixture
def server() -> object:
    return mcp_module._build_server()


def _get_tool(server: object, name: str) -> object:
    tools = server._tool_manager._tools  # type: ignore[attr-defined]
    if name not in tools:
        raise KeyError(f"Tool {name!r} not registered. Available: {list(tools)}")
    return tools[name]


def _call_tool(tool: Any, kwargs: dict[str, Any]) -> Any:
    """FastMCP tools store the original function on either ``.fn`` or
    ``.func`` depending on the SDK version. Try both."""
    fn = getattr(tool, "fn", None) or getattr(tool, "func", None)
    if fn is None:
        # Last resort: look for any callable attribute.
        for attr_name in ("handler", "_func", "_fn", "callback"):
            fn = getattr(tool, attr_name, None)
            if callable(fn):
                break
    assert callable(fn), f"Cannot find callable on {tool!r} attrs: {dir(tool)[:20]}"
    return fn(**kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAgentExtractTool:
    @pytest.mark.asyncio
    async def test_returns_serialized_result(
        self, server: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Monkeypatch the agent_extract import inside the tool function.
        async def fake_extract(
            url: str,
            schema: Any,
            *,
            instruction: str | None = None,
            config: Any = None,
            **overrides: Any,
        ) -> AgentResult:
            return AgentResult(
                mode="extract",
                data={"title": "Hello"},
                final_url=url,
                rendered_markdown="# Hello",
                screenshots=None,
                actions=[],
                tokens_used=42,
                blocked=False,
                error=None,
                duration_s=1.0,
                steps_used=1,
            )

        # Inject into both the agent package and any cached reference.
        from scrapper_tool import agent

        monkeypatch.setattr(agent, "agent_extract", fake_extract)

        tool = _get_tool(server, "agent_extract")
        payload = await _call_tool(
            tool,
            {
                "url": "https://example.com",
                "schema_json": {"type": "object"},
                "instruction": "extract title",
            },
        )
        assert payload["data"] == {"title": "Hello"}
        assert payload["mode"] == "extract"
        assert payload["blocked"] is False
        assert payload["rendered_markdown"] == "# Hello"
        assert payload["steps_used"] == 1
        assert payload["tokens_used"] == 42

    @pytest.mark.asyncio
    async def test_blocked_returns_envelope_not_raise(
        self, server: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_extract(*_: Any, **__: Any) -> AgentResult:
            raise AgentBlockedError("captcha hard-fail")

        from scrapper_tool import agent

        monkeypatch.setattr(agent, "agent_extract", fake_extract)

        tool = _get_tool(server, "agent_extract")
        payload = await _call_tool(tool, {"url": "https://example.com", "schema_json": None})
        assert payload["blocked"] is True
        assert "captcha hard-fail" in payload["error"]
        assert payload["data"] is None

    @pytest.mark.asyncio
    async def test_agent_error_returns_envelope(
        self, server: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_extract(*_: Any, **__: Any) -> AgentResult:
            raise AgentError("ollama crashed")

        from scrapper_tool import agent

        monkeypatch.setattr(agent, "agent_extract", fake_extract)

        tool = _get_tool(server, "agent_extract")
        payload = await _call_tool(tool, {"url": "https://e.com", "schema_json": None})
        assert payload["blocked"] is False
        assert "ollama crashed" in payload["error"]


class TestAgentBrowseTool:
    @pytest.mark.asyncio
    async def test_screenshots_base64_encoded_and_capped(
        self, server: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a 5-screenshot result; tool should keep only 3 base64
        # entries.
        screenshots = [b"fake-png-" + str(i).encode() for i in range(5)]

        async def fake_browse(*_: Any, **__: Any) -> AgentResult:
            return AgentResult(
                mode="browse",
                data={"title": "x"},
                final_url="https://e.com",
                screenshots=screenshots,
                actions=[],
                tokens_used=0,
                blocked=False,
                error=None,
                duration_s=2.0,
                steps_used=5,
            )

        from scrapper_tool import agent

        monkeypatch.setattr(agent, "agent_browse", fake_browse)

        tool = _get_tool(server, "agent_browse")
        payload = await _call_tool(tool, {"url": "https://e.com", "instruction": "do something"})
        assert payload["mode"] == "browse"
        # Cap is 3; each entry is base64 string.
        assert len(payload["screenshots"]) == 3
        for s in payload["screenshots"]:
            assert isinstance(s, str)
            import base64

            base64.b64decode(s)  # must decode without error


class TestAgentResultPayloadHelper:
    def test_dom_snippet_dropped_after_step_5(self) -> None:
        from scrapper_tool.agent.types import ActionTrace

        actions = [
            ActionTrace(
                step=i,
                action="click",
                target=f"#x{i}",
                screenshot_idx=None,
                dom_snippet="A" * 200,
                latency_ms=10,
            )
            for i in range(1, 9)
        ]
        result = AgentResult(
            mode="browse",
            data={},
            final_url="https://e.com",
            screenshots=None,
            actions=actions,
            tokens_used=0,
            blocked=False,
            error=None,
            duration_s=0.0,
            steps_used=8,
        )
        payload = mcp_module._agent_result_payload(result)
        # Steps 1-5 keep dom_snippet; 6-8 drop it.
        for entry in payload["actions"][:5]:
            assert entry["dom_snippet"] is not None
        for entry in payload["actions"][5:]:
            assert entry["dom_snippet"] is None

    def test_truncation_marker_set_when_markdown_oversized(self) -> None:
        big_md = "M" * (mcp_module._BODY_TRUNCATION_BYTES + 1024)
        result = AgentResult(
            mode="extract",
            data=None,
            final_url="https://e.com",
            rendered_markdown=big_md,
            actions=[],
            tokens_used=0,
            blocked=False,
            error=None,
            duration_s=0.0,
            steps_used=1,
        )
        payload = mcp_module._agent_result_payload(result)
        assert payload["rendered_markdown_truncated"] is True
        assert payload["rendered_markdown"] is not None
        assert len(payload["rendered_markdown"]) <= mcp_module._BODY_TRUNCATION_BYTES + 4

    def test_error_envelope_shape(self) -> None:
        env = mcp_module._agent_error_payload("oops", blocked=False)
        assert env["data"] is None
        assert env["error"] == "oops"
        assert env["blocked"] is False
        assert env["actions"] == []
