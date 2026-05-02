"""E2E test 3.G -- Captcha cascade Tier 2 (paid solver).

COSTS REAL MONEY. Set ``SCRAPPER_TOOL_CAPTCHA_KEY`` to your CapSolver /
NopeCHA / 2Captcha API key first. Skips automatically if no key is set.

Targets the public reCAPTCHA demo -- a kind only Tier 2 can solve.
"""

from __future__ import annotations

import asyncio
import os
import sys

from scrapper_tool.agent import AgentConfig, agent_extract


async def main() -> None:
    if not os.environ.get("SCRAPPER_TOOL_CAPTCHA_KEY"):
        print(
            "Captcha Tier 2 (skipped) -- set SCRAPPER_TOOL_CAPTCHA_KEY to run.\n"
            "            Default paid solver is CapSolver; override with "
            "SCRAPPER_TOOL_CAPTCHA_PAID_FALLBACK=nopecha|twocaptcha."
        )
        sys.exit(0)

    cfg = AgentConfig.from_env().merged(
        browser="patchright",
        captcha_solver="auto",
        timeout_s=300.0,
    )

    result = await agent_extract(
        "https://nopecha.com/demo/recaptcha",
        schema=(
            "Return a short JSON object describing whether the page passed the reCAPTCHA challenge."
        ),
        config=cfg,
    )

    print(
        f"Captcha Tier 2 [OK]  blocked={result.blocked} "
        f"duration={result.duration_s:.1f} s "
        f"data={result.data}"
    )
    print("Verify the solve count on your solver's dashboard.")


if __name__ == "__main__":
    asyncio.run(main())
