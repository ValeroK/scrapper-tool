"""E2E test 3.C -- Pattern C (CSS / microdata via selectolax).

Pure-string test -- doesn't hit the network. Verifies the microdata
extraction path used by ``patterns.c.extract_microdata_price``.
"""

from __future__ import annotations

from scrapper_tool.patterns.c import extract_microdata_price

HTML = """
<html><body>
  <span itemtype="http://schema.org/Offer">
    <meta itemprop="price" content="19.99">
    <meta itemprop="priceCurrency" content="USD">
  </span>
</body></html>
"""


def main() -> None:
    result = extract_microdata_price(HTML)
    assert result is not None, "microdata extractor returned None"
    price, currency = result
    assert str(price) == "19.99", price
    assert currency == "USD", currency
    print(f"Pattern C [OK]  price={price} currency={currency}")


if __name__ == "__main__":
    main()
