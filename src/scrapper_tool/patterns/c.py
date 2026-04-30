"""Pattern C — CSS / schema.org microdata price extraction.

Use when the price is rendered visibly in HTML but the page has no
embedded JSON state object (Pattern B not viable). Order of preference:

1. **schema.org microdata anchor** — ``<meta itemprop="price">`` +
   ``<meta itemprop="priceCurrency">``. Stable across CSS reshuffles
   because the anchors are semantic, not visual. Always try this first.
2. **Bespoke CSS selectors** — only when microdata is absent. Build the
   selector against the rendered DOM in DevTools; refine until robust
   to ancestor reshuffling.

Pattern B (``patterns.b.extract_product_offer``) handles the *full*
Product+Offer microdata case (entire ``itemtype="schema.org/Product"``
container with nested ``Offer``). Pattern C is the **lighter** case —
"there's a price somewhere in this HTML, find it" — for product pages
that don't ship a full schema.org block.

Backed by `selectolax` (lexbor backend; 30-40x faster than BeautifulSoup
at our fetch volumes).

Usage::

    from scrapper_tool.patterns.c import extract_microdata_price

    result = extract_microdata_price(html)
    if result:
        price, currency = result
        print(f"{price} {currency}")
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from selectolax.lexbor import LexborHTMLParser

from scrapper_tool._logging import get_logger

_logger = get_logger(__name__)


def _coerce_decimal(value: str | None) -> Decimal | None:
    """Best-effort string → Decimal, tolerating common price formatting.

    Accepts:
    - ``"19.99"`` — plain decimal
    - ``"$19.99"`` — leading currency glyphs (stripped)
    - ``" 19.99 "`` — leading/trailing whitespace
    - ``"1,299.99"`` — thousands-separator commas (stripped)
    - ``"19,99"`` — European decimal-comma is NOT supported here;
      vendor-specific normalisation lives in the consumer.

    Returns ``None`` for empty/non-numeric input.
    """
    if not value:
        return None
    cleaned = value.strip()
    # Strip common currency glyphs.
    for glyph in ("$", "€", "£", "₪", "¥"):
        cleaned = cleaned.replace(glyph, "")
    # Strip thousands-separator commas (US/UK convention; if you see
    # European comma-decimal you need vendor-side normalisation).
    cleaned = cleaned.replace(",", "")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def extract_microdata_price(html: str) -> tuple[Decimal, str] | None:
    """Find ``<meta itemprop="price">`` + ``<meta itemprop="priceCurrency">``.

    Returns ``(price, currency)`` or ``None`` when neither anchor is
    present. The price is read from the ``content`` attribute (preferred,
    avoids "$19.99 USD" parsing); falls back to text content if the
    anchor uses ``<span itemprop="price">19.99</span>`` instead.

    Both anchors must be findable; if ``itemprop="price"`` exists but
    ``itemprop="priceCurrency"`` is absent, returns ``None`` (the
    consumer needs both to do anything useful).
    """
    parser = LexborHTMLParser(html)

    price_node = parser.css_first('[itemprop="price"]')
    currency_node = parser.css_first('[itemprop="priceCurrency"]')
    if price_node is None or currency_node is None:
        _logger.debug("patterns.c.microdata.miss")  # type: ignore[unreachable]
        return None

    raw_price = price_node.attributes.get("content") or price_node.text(strip=True)
    raw_currency = currency_node.attributes.get("content") or currency_node.text(strip=True)

    price = _coerce_decimal(raw_price)
    if price is None or not raw_currency:
        _logger.warning(
            "patterns.c.microdata.bad_value",
            raw_price=raw_price,
            raw_currency=raw_currency,
        )
        return None

    _logger.info(
        "patterns.c.microdata.match",
        price=str(price),
        currency=raw_currency,
    )
    return price, raw_currency.strip()


def extract_via_selectors(
    html: str,
    *,
    price_selector: str,
    currency_selector: str | None = None,
    default_currency: str | None = None,
) -> tuple[Decimal, str] | None:
    """Last-resort bespoke CSS-selector extraction.

    Use only when microdata is absent. The caller supplies a price
    selector matching the price's element; optionally a currency
    selector or a hardcoded ``default_currency`` (for sites that don't
    surface the currency in a separate element).

    Returns ``(price, currency)`` or ``None`` if either is unresolvable.

    Parameters
    ----------
    price_selector : str
        CSS selector for the element containing the price text. The
        text content (after ``.strip()``) is parsed; common currency
        glyphs are stripped before parsing.
    currency_selector : str, optional
        Selector for a separate currency element. If absent,
        ``default_currency`` must be provided.
    default_currency : str, optional
        Hardcoded currency code (e.g. ``"USD"``) when the page doesn't
        surface it in markup.

    Examples
    --------
    >>> extract_via_selectors(
    ...     html,
    ...     price_selector=".product-price__amount",
    ...     default_currency="ILS",
    ... )
    (Decimal('299.00'), 'ILS')

    Raises
    ------
    ValueError
        If neither ``currency_selector`` nor ``default_currency`` was
        supplied — the caller has to pick one.
    """
    if currency_selector is None and default_currency is None:
        msg = "must supply either currency_selector or default_currency"
        raise ValueError(msg)

    parser = LexborHTMLParser(html)

    price_node = parser.css_first(price_selector)
    if price_node is None:
        _logger.debug("patterns.c.selector.miss", selector=price_selector)  # type: ignore[unreachable]
        return None

    raw_price = (
        price_node.attributes.get("content")
        or price_node.attributes.get("data-price")
        or price_node.text(strip=True)
    )
    price = _coerce_decimal(raw_price)
    if price is None:
        _logger.warning(
            "patterns.c.selector.bad_price",
            selector=price_selector,
            raw=raw_price,
        )
        return None

    currency: str | None
    if currency_selector is not None:
        currency_node = parser.css_first(currency_selector)
        if currency_node is None:
            _logger.debug(  # type: ignore[unreachable]
                "patterns.c.selector.miss_currency",
                selector=currency_selector,
            )
            return None
        currency = currency_node.attributes.get("content") or currency_node.text(strip=True)
    else:
        currency = default_currency

    if not currency:
        return None

    _logger.info(
        "patterns.c.selector.match",
        price=str(price),
        currency=currency,
    )
    return price, currency.strip()


__all__ = [
    "extract_microdata_price",
    "extract_via_selectors",
]
