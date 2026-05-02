"""E2E test 3.B -- Pattern B (embedded JSON via extruct).

Fetches schema.org's developer reference and tries to walk a Product+Offer
block. If the page rotates and no longer carries one, prints a hint --
swap to a real product page on a site you have permission to scrape.
"""

from __future__ import annotations

import asyncio

from scrapper_tool import request_with_retry, vendor_client
from scrapper_tool.patterns.b import extract_product_offer

URL = "https://schema.org/Product"


async def main() -> None:
    async with vendor_client() as client:
        resp = await request_with_retry(client, "GET", URL)

    product = extract_product_offer(resp.text, base_url=URL)
    if product is None:
        print(
            "Pattern B [WARN]  schema.org/Product rotated; no Product JSON-LD today.\n"
            "Re-run against a real product URL on a site you can scrape."
        )
        return

    assert product.name, "product.name is empty"
    print(
        f"Pattern B [OK]  name={product.name!r} price={product.price} currency={product.currency}"
    )


if __name__ == "__main__":
    asyncio.run(main())
