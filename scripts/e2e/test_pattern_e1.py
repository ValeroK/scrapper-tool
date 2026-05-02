"""E2E test 3.E1 -- ``agent_extract`` against quotes.toscrape.com.

Heavy: launches a stealth browser AND calls the local LLM. Configure via
``SCRAPPER_TOOL_AGENT_*`` env vars (see docs/E2E_TEST_PLAN.md §2.3).

Defaults to Patchright + LM Studio for fast iteration. Override with
``SCRAPPER_TOOL_AGENT_BROWSER=camoufox`` for the highest-stealth backend.
"""

from __future__ import annotations

import asyncio
import os

from scrapper_tool.agent import AgentConfig, agent_extract

SCHEMA = {
    "type": "object",
    "properties": {
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "author": {"type": "string"},
                },
                "required": ["text", "author"],
            },
        }
    },
    "required": ["quotes"],
}


async def main() -> None:
    cfg = AgentConfig.from_env().merged(
        browser=os.environ.get("SCRAPPER_TOOL_AGENT_BROWSER", "patchright"),
        captcha_solver="none",
        timeout_s=180.0,
    )

    result = await agent_extract(
        "https://quotes.toscrape.com/",
        schema=SCHEMA,
        config=cfg,
        instruction="Extract every quote on the page.",
    )

    assert not result.blocked, f"unexpected block: {result.error}"
    assert result.data is not None, "no data returned"
    quotes = result.data["quotes"] if isinstance(result.data, dict) else result.data
    assert isinstance(quotes, list), f"expected list, got {type(quotes).__name__}"
    assert len(quotes) >= 3, f"expected >=3 quotes, got {len(quotes)}"

    print(f"Pattern E1 [OK]  extracted {len(quotes)} quotes in {result.duration_s:.1f} s")
    print(f"            backend={cfg.browser} model={cfg.model}")
    print(f"            first quote: {quotes[0]}")


if __name__ == "__main__":
    asyncio.run(main())
