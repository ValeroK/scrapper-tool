"""E2E test 3.E2 -- ``agent_browse`` (multi-step interactive agent).

Heavy: 5-15 LLM calls across the agent loop. Asks the agent to navigate
to page 2 of quotes.toscrape.com and report a count.

Local 8B models sometimes return slightly off-shape JSON -- re-run if so.
"""

from __future__ import annotations

import asyncio
import os

from scrapper_tool.agent import AgentConfig, agent_browse


async def main() -> None:
    cfg = AgentConfig.from_env().merged(
        browser=os.environ.get("SCRAPPER_TOOL_AGENT_BROWSER", "patchright"),
        captcha_solver="none",
        max_steps=10,
        timeout_s=240.0,
    )

    result = await agent_browse(
        "https://quotes.toscrape.com/",
        instruction=(
            "Click the 'Next' button at the bottom of the page to go to "
            'page 2, then return a JSON object {"page": 2, "count": '
            "<number of quotes shown on page 2>}."
        ),
        config=cfg,
    )

    assert not result.blocked, f"unexpected block: {result.error}"
    print(
        f"Pattern E2 [OK]  steps={result.steps_used} "
        f"duration={result.duration_s:.1f} s "
        f"final_url={result.final_url} "
        f"data={result.data}"
    )


if __name__ == "__main__":
    asyncio.run(main())
