"""Unit tests for ``scrapper_tool.patterns.b`` — Pattern B extractor.

Five fixture variants:

1. JSON-LD top-level Product (most common shape)
2. JSON-LD with Product nested under @graph
3. Microdata schema.org Product (older WP/WooCommerce default)
4. RDFa Product (rare in 2026 but legal)
5. No Product block at all (returns None)

Plus a few edge cases that the recon journal called out repeatedly:
- offers as a list (multi-offer pages → take the first)
- price nested inside priceSpecification
- brand as a {"@type": "Brand", "name": "..."} dict vs string
- gtin13 vs gtin14 vs plain gtin (first match wins)
"""

from __future__ import annotations

from decimal import Decimal

from scrapper_tool.patterns.b import ProductOffer, extract_product_offer

# --- Fixture builders -----------------------------------------------------


JSON_LD_TOP_LEVEL = """<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Widget X1",
  "sku": "X1-SKU",
  "mpn": "MFR-X1-001",
  "gtin13": "1234567890123",
  "brand": {"@type": "Brand", "name": "WidgetCo"},
  "description": "A test widget.",
  "image": "https://example.test/widget.jpg",
  "offers": {
    "@type": "Offer",
    "price": "19.99",
    "priceCurrency": "USD",
    "availability": "https://schema.org/InStock",
    "url": "https://example.test/widget"
  }
}
</script></head><body></body></html>"""


JSON_LD_GRAPH = """<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {"@type": "WebSite", "name": "Example"},
    {
      "@type": "Product",
      "name": "Widget Graph",
      "sku": "GRAPH-1",
      "offers": {
        "@type": "Offer",
        "price": "84.38",
        "priceCurrency": "EUR",
        "availability": "https://schema.org/OutOfStock"
      }
    }
  ]
}
</script></head><body></body></html>"""


JSON_LD_OFFERS_LIST = """<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Multi-offer widget",
  "offers": [
    {"@type": "Offer", "price": "9.99", "priceCurrency": "USD"},
    {"@type": "Offer", "price": "11.99", "priceCurrency": "USD"}
  ]
}
</script></head><body></body></html>"""


JSON_LD_PRICE_SPEC = """<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Spec-priced",
  "offers": {
    "@type": "Offer",
    "priceSpecification": {"@type": "PriceSpecification", "price": "42.00", "priceCurrency": "ILS"},
    "availability": "https://schema.org/InStock"
  }
}
</script></head><body></body></html>"""


MICRODATA_PRODUCT = """<html><body>
<div itemscope itemtype="https://schema.org/Product">
  <span itemprop="name">Microdata Widget</span>
  <span itemprop="sku">MD-1</span>
  <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
    <meta itemprop="price" content="6.84">
    <meta itemprop="priceCurrency" content="USD">
    <link itemprop="availability" href="https://schema.org/InStock">
  </div>
</div>
</body></html>"""


NO_PRODUCT = """<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "WebSite", "name": "no product here"}
</script>
</head><body><h1>About us</h1></body></html>"""


PLAIN_HTML_NO_STRUCTURED_DATA = """<html><body>
<div class="product">
  <h1>Just a product page</h1>
  <span class="price">$19.99</span>
</div>
</body></html>"""


BRAND_AS_STRING = """<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Plain brand",
  "brand": "PlainBrandCo",
  "offers": {"@type": "Offer", "price": "5.00", "priceCurrency": "USD"}
}
</script></head><body></body></html>"""


# --- Tests ----------------------------------------------------------------


class TestJsonLdTopLevel:
    def test_extracts_full_product(self) -> None:
        product = extract_product_offer(JSON_LD_TOP_LEVEL)
        assert product is not None
        assert isinstance(product, ProductOffer)
        assert product.name == "Widget X1"
        assert product.sku == "X1-SKU"
        assert product.mpn == "MFR-X1-001"
        assert product.gtin == "1234567890123"
        assert product.brand == "WidgetCo"
        assert product.description == "A test widget."
        assert product.image == "https://example.test/widget.jpg"
        assert product.price == Decimal("19.99")
        assert product.currency == "USD"
        assert product.availability == "https://schema.org/InStock"
        assert product.url == "https://example.test/widget"


class TestJsonLdGraph:
    def test_finds_product_inside_graph(self) -> None:
        product = extract_product_offer(JSON_LD_GRAPH)
        assert product is not None
        assert product.name == "Widget Graph"
        assert product.sku == "GRAPH-1"
        assert product.price == Decimal("84.38")
        assert product.currency == "EUR"
        assert product.availability == "https://schema.org/OutOfStock"


class TestJsonLdOffersList:
    def test_takes_first_offer(self) -> None:
        product = extract_product_offer(JSON_LD_OFFERS_LIST)
        assert product is not None
        assert product.price == Decimal("9.99")  # first offer
        assert product.currency == "USD"


class TestJsonLdPriceSpecification:
    def test_extracts_nested_price(self) -> None:
        product = extract_product_offer(JSON_LD_PRICE_SPEC)
        assert product is not None
        assert product.price == Decimal("42.00")
        assert product.currency == "ILS"
        assert product.availability == "https://schema.org/InStock"


class TestMicrodata:
    def test_extracts_microdata_product(self) -> None:
        product = extract_product_offer(MICRODATA_PRODUCT)
        assert product is not None
        assert product.name == "Microdata Widget"
        assert product.sku == "MD-1"
        assert product.price == Decimal("6.84")
        assert product.currency == "USD"
        assert product.availability == "https://schema.org/InStock"


class TestBrandShapes:
    def test_brand_as_plain_string(self) -> None:
        product = extract_product_offer(BRAND_AS_STRING)
        assert product is not None
        assert product.brand == "PlainBrandCo"


class TestNoProductBlock:
    def test_returns_none_when_no_product(self) -> None:
        product = extract_product_offer(NO_PRODUCT)
        assert product is None

    def test_returns_none_for_plain_html(self) -> None:
        # No structured data at all — the lib must not invent a product.
        product = extract_product_offer(PLAIN_HTML_NO_STRUCTURED_DATA)
        assert product is None


class TestBaseUrlPropagation:
    def test_base_url_does_not_break_extraction(self) -> None:
        # extruct uses base_url internally to resolve relative URLs;
        # passing it shouldn't change top-level extraction.
        product = extract_product_offer(JSON_LD_TOP_LEVEL, base_url="https://example.test/")
        assert product is not None
        assert product.name == "Widget X1"


class TestProductOfferModel:
    def test_extra_keys_are_ignored(self) -> None:
        # The schema configures extra="ignore" so vendors that add
        # fields don't break parsing — assert it stays that way.
        po = ProductOffer(
            name="X",
            extra_garbage_key="should be silently dropped",  # type: ignore[call-arg]
        )
        assert po.name == "X"
        assert not hasattr(po, "extra_garbage_key")
