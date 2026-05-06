"""JSON-LD Product extractor — schema.org/Product blocks via :mod:`patterns.b`.

Two outputs combined:

1. The auto-detected ``ProductOffer`` (``patterns.b.extract_product_offer``)
   — the sidecar's normalised offer shape. Single dict.
2. The raw ``json-ld`` syntax dump from ``extruct`` — list of every
   LD+JSON block on the page. Useful when the page emits ProductGroup,
   ItemList, or per-result Product blocks that the auto-detector
   doesn't pick a winner from.

Either is enough to count as a structured signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scrapper_tool._extractors import ExtractorResult, register


@dataclass
class JsonLdProductExtractor:
    name: str = "json_ld_product"

    def extract(
        self,
        html: str,
        *,
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ExtractorResult:
        del options  # No per-call options; signature kept for protocol parity.

        from scrapper_tool.patterns.b import extract_product_offer  # noqa: PLC0415

        product_obj = extract_product_offer(html, base_url=base_url)
        product = product_obj.model_dump(mode="json") if product_obj is not None else None

        json_ld: list[Any] | None = None
        try:
            import extruct  # noqa: PLC0415

            raw = extruct.extract(html, base_url=base_url, syntaxes=["json-ld"], uniform=True)
            json_ld = raw.get("json-ld") or None
        except Exception:
            json_ld = None

        if product is None and not json_ld:
            return ExtractorResult.empty(self.name)

        return ExtractorResult(
            data={"product": product, "json_ld": json_ld},
            has_signal=True,
            extractor_name=self.name,
        )


register(JsonLdProductExtractor())
