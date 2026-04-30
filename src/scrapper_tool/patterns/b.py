"""Pattern B — Embedded JSON / schema.org Product extractor.

Most modern e-commerce sites embed structured product data directly in
the HTML response. Five common shapes:

1. ``<script type="application/ld+json">`` with ``"@type": "Product"`` —
   schema.org JSON-LD. Most prevalent.
2. JSON-LD with the Product nested under ``"@graph": [...]``.
3. Schema.org **microdata** (``<div itemscope itemtype="https://schema.org/Product">``
   with ``<meta itemprop="...">`` children).
4. Schema.org **RDFa** (much rarer in 2026 but still legal).
5. Framework-specific blobs — ``__NEXT_DATA__``, ``__NUXT__``,
   ``window.__INITIAL_STATE__``, ``self.__next_f.push(...)``. Not
   handled here; consumers should still hand-extract those because
   their shape is vendor-specific.

This module covers shapes 1-4 via the ``extruct`` library, called with
``uniform=True`` so all three syntaxes (json-ld / microdata / rdfa)
return the same shape — one walker handles them all.

Usage::

    from scrapper_tool.patterns.b import extract_product_offer

    product = extract_product_offer(html, base_url="https://example.com/")
    if product:
        print(product.price, product.currency, product.availability)
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import extruct  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from scrapper_tool._logging import get_logger

_logger = get_logger(__name__)


class ProductOffer(BaseModel):
    """Normalised schema.org Product + Offer.

    Fields are the union of what we typically read across vendors;
    most are ``Optional`` because vendors omit fields liberally and
    parser strictness is the wrong default for a recon helper.

    The ``availability`` field carries the raw schema.org URI
    (``https://schema.org/InStock`` / ``OutOfStock`` / ``Discontinued`` /
    etc.) — consumers map it to their own stock-status enum.
    """

    name: str | None = Field(default=None)
    sku: str | None = Field(default=None)
    mpn: str | None = Field(
        default=None,
        description="Manufacturer Part Number — often the OEM in automotive use-cases.",
    )
    gtin: str | None = Field(
        default=None,
        description="GTIN-8 / 12 / 13 / 14 (whichever the vendor supplied).",
    )
    brand: str | None = Field(default=None)
    description: str | None = Field(default=None)
    image: str | None = Field(default=None)

    price: Decimal | None = Field(default=None)
    currency: str | None = Field(
        default=None,
        description="ISO 4217 currency code (e.g. 'USD', 'ILS').",
    )
    availability: str | None = Field(
        default=None,
        description="Raw schema.org availability URI.",
    )
    url: str | None = Field(default=None, description="Canonical Product URL.")

    model_config = {"extra": "ignore"}


# --- Internal walkers (operate on extruct's uniform=True normalised shape) -


def _walk_for_product(node: Any) -> dict[str, Any] | None:
    """Depth-first search through arbitrarily-nested JSON-LD-shaped data
    for a Product node.

    Handles the three common shapes after ``extruct.extract(..., uniform=True)``:
    - top-level dict with ``@type == "Product"``
    - dict with ``"@graph": [...]`` containing the Product
    - list of dicts (multiple structured-data blocks in one page)
    """
    if isinstance(node, dict):
        node_type = node.get("@type")
        if node_type == "Product" or (
            isinstance(node_type, list) and "Product" in node_type
        ):
            return node
        # Recurse through @graph / itemListElement / mainEntity / etc.
        for value in node.values():
            found = _walk_for_product(value)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for item in node:
            found = _walk_for_product(item)
            if found is not None:
                return found
    return None


def _coerce_decimal(value: Any) -> Decimal | None:
    """Best-effort string/number → Decimal."""
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _first_gtin(product: dict[str, Any]) -> str | None:
    """Return whichever ``gtin*`` key the vendor supplied first."""
    for key in ("gtin", "gtin13", "gtin14", "gtin12", "gtin8"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _flatten_brand(raw_brand: Any) -> str | None:
    """Brand can be a string, a {"@type": "Brand", "name": ...} dict, or a list."""
    if isinstance(raw_brand, list) and raw_brand:
        raw_brand = raw_brand[0]
    if isinstance(raw_brand, str):
        return raw_brand.strip() or None
    if isinstance(raw_brand, dict):
        name = raw_brand.get("name")
        if isinstance(name, str):
            return name.strip() or None
    return None


def _flatten_image(raw_image: Any) -> str | None:
    """Image can be a string, list[str], or {"@type": "ImageObject", "url": ...}."""
    if isinstance(raw_image, list) and raw_image:
        raw_image = raw_image[0]
    if isinstance(raw_image, dict):
        raw_image = raw_image.get("url")
    return raw_image if isinstance(raw_image, str) else None


def _to_product_offer(product: dict[str, Any]) -> ProductOffer:
    """Convert a Product node (post-uniform-normalisation) to ``ProductOffer``."""
    # Offers can be: a dict, a list of dicts, or absent.
    raw_offers = product.get("offers")
    offer: dict[str, Any] = {}
    if isinstance(raw_offers, dict):
        offer = raw_offers
    elif isinstance(raw_offers, list) and raw_offers:
        # Take the first; vendors that publish multiple offers usually
        # list them in price-ascending order — close enough for "give
        # me a price" semantics. Consumers wanting all offers should
        # call extruct themselves.
        first = raw_offers[0]
        if isinstance(first, dict):
            offer = first

    # Some vendors nest the price + currency inside priceSpecification.
    raw_price = offer.get("price")
    raw_currency = offer.get("priceCurrency")
    spec = offer.get("priceSpecification")
    if isinstance(spec, dict):
        if raw_price is None:
            raw_price = spec.get("price")
        if raw_currency is None:
            raw_currency = spec.get("priceCurrency")

    name = product.get("name")
    sku = product.get("sku")
    mpn = product.get("mpn")
    description = product.get("description")
    offer_url = offer.get("url")
    product_url = product.get("url")
    availability = offer.get("availability")
    chosen_url = offer_url if isinstance(offer_url, str) else product_url

    return ProductOffer(
        name=name if isinstance(name, str) else None,
        sku=sku if isinstance(sku, str) else None,
        mpn=mpn if isinstance(mpn, str) else None,
        gtin=_first_gtin(product),
        brand=_flatten_brand(product.get("brand")),
        description=description if isinstance(description, str) else None,
        image=_flatten_image(product.get("image")),
        price=_coerce_decimal(raw_price),
        currency=raw_currency if isinstance(raw_currency, str) else None,
        availability=availability if isinstance(availability, str) else None,
        url=chosen_url if isinstance(chosen_url, str) else None,
    )


# --- Public entrypoint -----------------------------------------------------


def extract_product_offer(
    html: str,
    base_url: str | None = None,
) -> ProductOffer | None:
    """Extract the first schema.org Product+Offer from ``html``.

    Tries (in order):

    1. JSON-LD — most common modern shape.
    2. Microdata — older sites and WordPress-WooCommerce defaults.
    3. RDFa — rare in 2026 but spec-legal and supported.

    All three use ``extruct.extract(..., uniform=True)`` which normalises
    the output to JSON-LD shape, so the walker is shared.

    Returns ``None`` if no Product block is present in any syntax.

    Parameters
    ----------
    html : str
        Raw HTML response body.
    base_url : str, optional
        Used by extruct to resolve relative URLs (e.g. for JSON-LD
        ``@id`` references). Pass ``str(response.url)`` from your
        ``httpx`` response when available.

    Returns
    -------
    :class:`ProductOffer` or ``None``.
    """
    raw = extruct.extract(
        html,
        base_url=base_url,
        syntaxes=["json-ld", "microdata", "rdfa"],
        uniform=True,
    )

    # uniform=True means all three syntaxes return JSON-LD-shaped lists,
    # so one walker covers everything.
    for syntax in ("json-ld", "microdata", "rdfa"):
        items = raw.get(syntax) or []
        if not items:
            continue
        product = _walk_for_product(items)
        if product is not None:
            _logger.info("patterns.b.match", syntax=syntax)
            return _to_product_offer(product)

    _logger.debug("patterns.b.no_product_block")
    return None


__all__ = [
    "ProductOffer",
    "extract_product_offer",
]
