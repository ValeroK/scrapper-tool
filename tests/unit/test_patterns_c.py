"""Unit tests for ``scrapper_tool.patterns.c`` — Pattern C extractor.

Covers:
- ``extract_microdata_price`` happy path (`<meta>` content attribute).
- ``extract_microdata_price`` fallback to text content (`<span itemprop="price">19.99</span>`).
- Missing microdata returns None.
- Missing currency anchor returns None even when price is present.
- ``extract_via_selectors`` happy path with default_currency.
- ``extract_via_selectors`` happy path with currency_selector.
- ``extract_via_selectors`` missing price element returns None.
- ``extract_via_selectors`` raises ValueError without currency_selector OR default_currency.
- ``_coerce_decimal`` handles glyphs ($/€/£/₪) and thousands separators.
- Selector extracts from ``content`` / ``data-price`` attributes when present.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scrapper_tool.patterns.c import (
    extract_microdata_price,
    extract_via_selectors,
)

# --- Microdata fixtures --------------------------------------------------


MICRODATA_META_TAGS = """<html><body>
<div class="product">
  <h1>Lighter case widget</h1>
  <meta itemprop="price" content="6.84">
  <meta itemprop="priceCurrency" content="USD">
</div>
</body></html>"""


MICRODATA_TEXT_CONTENT = """<html><body>
<span itemprop="price">19.99</span>
<span itemprop="priceCurrency">EUR</span>
</body></html>"""


MICRODATA_PRICE_ONLY = """<html><body>
<meta itemprop="price" content="42.00">
</body></html>"""


NO_MICRODATA = """<html><body>
<div class="price">$5.00</div>
</body></html>"""


# --- Selector fixtures ---------------------------------------------------


SELECTOR_PRICE_TEXT = """<html><body>
<div class="amount">$299.00</div>
</body></html>"""


SELECTOR_PRICE_WITH_CURRENCY_ELEMENT = """<html><body>
<div class="price-block">
  <span class="amount">1,299.99</span>
  <span class="currency">USD</span>
</div>
</body></html>"""


SELECTOR_PRICE_DATA_ATTRIBUTE = """<html><body>
<div class="amount" data-price="84.38">$84.38</div>
</body></html>"""


# --- Microdata tests -----------------------------------------------------


class TestMicrodataPrice:
    def test_meta_content_attribute_path(self) -> None:
        result = extract_microdata_price(MICRODATA_META_TAGS)
        assert result == (Decimal("6.84"), "USD")

    def test_text_content_fallback(self) -> None:
        result = extract_microdata_price(MICRODATA_TEXT_CONTENT)
        assert result == (Decimal("19.99"), "EUR")

    def test_missing_microdata_returns_none(self) -> None:
        assert extract_microdata_price(NO_MICRODATA) is None

    def test_price_without_currency_returns_none(self) -> None:
        # Both anchors required — no graceful degradation, since
        # consumers can't do anything useful with a price-no-currency.
        assert extract_microdata_price(MICRODATA_PRICE_ONLY) is None


# --- Selector tests ------------------------------------------------------


class TestSelectorWithDefaultCurrency:
    def test_extracts_price_strips_glyph(self) -> None:
        result = extract_via_selectors(
            SELECTOR_PRICE_TEXT,
            price_selector=".amount",
            default_currency="USD",
        )
        assert result == (Decimal("299.00"), "USD")

    def test_missing_element_returns_none(self) -> None:
        result = extract_via_selectors(
            SELECTOR_PRICE_TEXT,
            price_selector=".does-not-exist",
            default_currency="USD",
        )
        assert result is None


class TestSelectorWithCurrencyElement:
    def test_reads_currency_from_separate_element(self) -> None:
        result = extract_via_selectors(
            SELECTOR_PRICE_WITH_CURRENCY_ELEMENT,
            price_selector=".amount",
            currency_selector=".currency",
        )
        assert result == (Decimal("1299.99"), "USD")  # comma stripped

    def test_missing_currency_element_returns_none(self) -> None:
        result = extract_via_selectors(
            SELECTOR_PRICE_WITH_CURRENCY_ELEMENT,
            price_selector=".amount",
            currency_selector=".does-not-exist",
        )
        assert result is None


class TestSelectorAttributePreferred:
    def test_data_price_attribute_used_when_present(self) -> None:
        # The element shows "$84.38" as text but data-price="84.38" is
        # preferred (no glyph to strip; precise).
        result = extract_via_selectors(
            SELECTOR_PRICE_DATA_ATTRIBUTE,
            price_selector=".amount",
            default_currency="USD",
        )
        assert result == (Decimal("84.38"), "USD")


class TestSelectorContractValidation:
    def test_no_currency_source_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="currency_selector or default_currency"):
            extract_via_selectors(
                SELECTOR_PRICE_TEXT,
                price_selector=".amount",
            )


class TestPriceCoercion:
    """Spot-check the internal ``_coerce_decimal`` via the public surface."""

    @pytest.mark.parametrize(
        ("price_text", "expected"),
        [
            ("19.99", Decimal("19.99")),
            ("$19.99", Decimal("19.99")),
            ("€42.00", Decimal("42.00")),
            ("£99.50", Decimal("99.50")),
            ("₪123.45", Decimal("123.45")),
            ("¥1000.00", Decimal("1000.00")),
            ("  100.00  ", Decimal("100.00")),
            ("1,299.99", Decimal("1299.99")),
        ],
    )
    def test_glyphs_and_separators_stripped(self, price_text: str, expected: Decimal) -> None:
        html = (
            f'<div><span itemprop="price">{price_text}</span>'
            f'<span itemprop="priceCurrency">USD</span></div>'
        )
        result = extract_microdata_price(html)
        assert result is not None
        assert result[0] == expected

    @pytest.mark.parametrize(
        "price_text",
        ["", "not a price", "abc"],
    )
    def test_unparseable_returns_none(self, price_text: str) -> None:
        html = (
            f'<div><span itemprop="price">{price_text}</span>'
            f'<span itemprop="priceCurrency">USD</span></div>'
        )
        result = extract_microdata_price(html)
        assert result is None
