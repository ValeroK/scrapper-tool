"""E2E test 3.K -- error taxonomy.

Two contracts the rest of the codebase relies on:

1. ``AgentBlockedError`` is caught by ``except BlockedError`` (multi-inherit)
   so existing consumer code stays compatible.
2. An unreachable LLM URL surfaces as ``AgentLLMError`` at session start
   (probe-on-entry), not silently mid-run.
"""

from __future__ import annotations

import asyncio

from scrapper_tool import BlockedError
from scrapper_tool.agent import AgentConfig, agent_extract
from scrapper_tool.errors import AgentBlockedError, AgentLLMError


def test_multi_inheritance() -> None:
    try:
        raise AgentBlockedError("simulated stealth failure")
    except BlockedError as exc:
        print(f"errors [OK]  AgentBlockedError caught by BlockedError: {exc}")


async def test_bad_llm_url() -> None:
    cfg = AgentConfig(
        llm="openai_compat",
        ollama_url="http://127.0.0.1:1",  # nothing should listen here
        model="qwen3-vl-8b-instruct",
        captcha_solver="none",
        browser="patchright",
        timeout_s=10.0,
    )
    try:
        await agent_extract("https://example.com", schema={}, config=cfg)
    except AgentLLMError as exc:
        print(f"errors [OK]  bad URL -> AgentLLMError: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"errors [FAIL]  expected AgentLLMError, got {type(exc).__name__}: {exc}")
        return
    print("errors [FAIL]  expected AgentLLMError, got nothing (the call succeeded?)")


async def main() -> None:
    test_multi_inheritance()
    await test_bad_llm_url()


if __name__ == "__main__":
    asyncio.run(main())
