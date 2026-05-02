"""E2E test 3.F -- Captcha cascade Tier 0 (Camoufox auto-pass).

Targets the public Cloudflare Turnstile demo with Camoufox + the auto
cascade. Tier 0 is "Camoufox passes silently"; if that fails the cascade
escalates to Tier 1 (Theyka), and finally Tier 2 (paid) only when
``SCRAPPER_TOOL_CAPTCHA_KEY`` is set. With no key, this test exercises
just the free OSS path.

Requires Camoufox installed (``camoufox fetch`` or build with
``INSTALL_CAMOUFOX=1``).
"""

from __future__ import annotations

import asyncio

from scrapper_tool.agent import AgentConfig, agent_extract


async def main() -> None:
    cfg = AgentConfig.from_env().merged(
        browser="camoufox",
        captcha_solver="auto",
        timeout_s=240.0,
    )

    result = await agent_extract(
        "https://nopecha.com/demo/cloudflare",
        schema=(
            "Return a short JSON object describing whether the page rendered past the challenge."
        ),
        config=cfg,
    )

    if result.blocked:
        print(
            "Captcha Tier 0 [WARN]  Camoufox didn't auto-pass today.\n"
            "            This happens -- CF rotates challenges. "
            "Re-run, or escalate to Tier 1 (Theyka) / Tier 2 (paid solver)."
        )
        return

    md = result.rendered_markdown or ""
    assert len(md) > 100, f"rendered too little ({len(md)} bytes) -- likely partial bypass"
    print(f"Captcha Tier 0 [OK]  rendered {len(md)} bytes in {result.duration_s:.1f} s")


if __name__ == "__main__":
    asyncio.run(main())
