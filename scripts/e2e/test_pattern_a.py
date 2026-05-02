"""E2E test 3.A -- Pattern A (anonymous JSON API).

Hits a public JSON mock and verifies the response shape. Validates the core
HTTP path (httpx + retry/backoff) without any pattern-specific extractor.
"""

from __future__ import annotations

import asyncio

from scrapper_tool import request_with_retry, vendor_client


async def main() -> None:
    async with vendor_client() as client:
        resp = await request_with_retry(client, "GET", "https://dummyjson.com/products/1")

    assert resp.status_code == 200, f"unexpected status {resp.status_code}"
    payload = resp.json()
    assert payload["id"] == 1, payload
    assert "title" in payload, payload
    print(f"Pattern A [OK]  product={payload['title']!r}")


if __name__ == "__main__":
    asyncio.run(main())
