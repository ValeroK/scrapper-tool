"""Open Graph product extractor — ``<meta property="og:product:*">`` tags.

Cheap fallback for ecommerce pages that don't emit LD+JSON Product
or microdata price. Many Shopify / Magento storefronts populate the
Facebook OG product surface even when the structured-data layer is
incomplete.

Targets the canonical OG fields:

* ``og:product:price:amount`` + ``og:product:price:currency``
* ``og:title`` (used as fallback ``name`` when LD+JSON absent)
* ``og:url`` (used as canonical URL)
* ``og:image``
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from scrapper_tool._extractors import ExtractorResult, register

_OG_META_RE = re.compile(
    r'<meta\s+[^>]*property\s*=\s*["\']og:([\w:]+)["\']\s+[^>]*content\s*=\s*["\']([^"\']*)["\']',
    re.IGNORECASE,
)
# Some pages put content first then property.
_OG_META_RE_ALT = re.compile(
    r'<meta\s+[^>]*content\s*=\s*["\']([^"\']*)["\']\s+[^>]*property\s*=\s*["\']og:([\w:]+)["\']',
    re.IGNORECASE,
)


def _scan(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _OG_META_RE.finditer(html):
        prop, value = match.group(1), match.group(2)
        out.setdefault(prop, value)
    for match in _OG_META_RE_ALT.finditer(html):
        value, prop = match.group(1), match.group(2)
        out.setdefault(prop, value)
    return out


@dataclass
class OpenGraphExtractor:
    name: str = "open_graph"

    def extract(
        self,
        html: str,
        *,
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ExtractorResult:
        del base_url, options

        og = _scan(html)
        # We only care about product-typed pages for the structured-signal
        # contract. Any title + price + currency triple counts.
        price = og.get("product:price:amount") or og.get("price:amount")
        currency = og.get("product:price:currency") or og.get("price:currency")
        if not (price and currency):
            return ExtractorResult.empty(self.name)

        return ExtractorResult(
            data={
                "title": og.get("title"),
                "url": og.get("url"),
                "image": og.get("image"),
                "price": str(price),
                "currency": currency,
            },
            has_signal=True,
            extractor_name=self.name,
        )


register(OpenGraphExtractor())
