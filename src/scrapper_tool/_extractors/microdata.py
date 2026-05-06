"""Microdata price extractor — ``<meta itemprop="price">`` + currency.

Lifted from ``patterns.c.extract_microdata_price``. Output shape kept
backward-compatible with the legacy ``_extract_b_c`` tuple's third
element.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scrapper_tool._extractors import ExtractorResult, register


@dataclass
class MicrodataPriceExtractor:
    name: str = "microdata_price"

    def extract(
        self,
        html: str,
        *,
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ExtractorResult:
        del base_url, options

        from scrapper_tool.patterns.c import extract_microdata_price  # noqa: PLC0415

        match = extract_microdata_price(html)
        if match is None:
            return ExtractorResult.empty(self.name)

        price, currency = match
        return ExtractorResult(
            data={"price": str(price), "currency": currency},
            has_signal=True,
            extractor_name=self.name,
        )


register(MicrodataPriceExtractor())
