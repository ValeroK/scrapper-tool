"""E2E test 3.D -- Pattern D (Scrapling hostile-site fetcher).

Heavy. Launches Playwright Chromium via Scrapling. Targets the public
Cloudflare Turnstile demo. Skip if you don't need Pattern D -- Pattern E1
(``test_pattern_e1.py``) handles the same site with Camoufox + LLM
extraction.

Requires ``[hostile]`` extra (or ``[full]``) installed.
"""

from __future__ import annotations

import asyncio

from scrapper_tool.patterns.d import hostile_client

URL = "https://nopecha.com/demo/cloudflare"


async def main() -> None:
    async with hostile_client(headless=True, block_resources=True) as fetcher:
        resp = await fetcher.async_fetch(URL, solve_cloudflare=True)

    status = getattr(resp, "status", None) or getattr(resp, "status_code", None)
    assert status == 200, f"unexpected status {status}"
    body = getattr(resp, "html_content", None) or getattr(resp, "text", "")
    assert len(body) > 1000, f"body too short -- likely blocked ({len(body)} bytes)"
    print(f"Pattern D [OK]  rendered {len(body)} bytes via Scrapling")


if __name__ == "__main__":
    asyncio.run(main())
