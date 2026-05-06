"""CSS-schema extractor — runs CSS selectors over raw HTML.

Implements a subset of Crawl4AI's ``JsonCssExtractionStrategy`` schema
shape so callers can express extraction as a structured dict instead
of writing per-vendor selectolax. Built directly on selectolax (a
core dep), so this extractor works regardless of which optional extras
are installed — no browser, no LLM, no Crawl4AI.

The schema shape::

    {
        "baseSelector": "div.search-result-item",
        "fields": [
            {"name": "title", "selector": "h3.product-title", "type": "text"},
            {"name": "price", "selector": "span.price", "type": "text"},
            {
                "name": "product_url",
                "selector": "a.product-link",
                "type": "attribute",
                "attribute": "href",
            },
            {"name": "vendor_item_id", "selector": "a", "type": "attribute",
             "attribute": "data-sku", "default": ""},
        ],
    }

Field types supported:

* ``text`` — matched element's ``.text(strip=True)``.
* ``attribute`` — matched element's attribute value. ``attribute`` key
  on the field spec selects which attribute. Returns the value as-is.
* ``html`` — matched element's inner HTML.
* ``int`` / ``float`` — convenience: extracts text and attempts numeric
  conversion (returns ``None`` on parse failure unless ``default`` set).

Field options:

* ``default`` — value to substitute when the selector doesn't match.
  When unset, the field is omitted from the output dict (sparse).
* ``optional`` (bool, default False) — when False, a missing required
  field drops the entire row. When True, the field is just absent.

Output: list of dicts, one per matched row from ``baseSelector``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from selectolax.lexbor import LexborHTMLParser, LexborNode

from scrapper_tool._extractors import ExtractorResult, register


def _extract_field(  # noqa: PLR0911 — small dispatch on field type
    node: LexborNode, field_spec: dict[str, Any]
) -> tuple[bool, Any]:
    """Pull one field out of a row node.

    Returns ``(found, value)``. ``found=False`` when the selector
    didn't match (caller decides whether to use ``default``).
    """
    selector = field_spec.get("selector")
    field_type = field_spec.get("type", "text")

    # selectolax stubs claim css_first returns LexborNode (non-None),
    # but the implementation can return None when nothing matches.
    # Cast to the optional type so the runtime None-check below is
    # not flagged as unreachable. Selector-less fields use the row
    # node directly (caller wants the whole row).
    match: LexborNode | None = (
        node
        if not selector
        else cast("LexborNode | None", node.css_first(selector))
    )
    if match is None:
        return False, None

    if field_type == "text":
        return True, match.text(strip=True) or None
    if field_type == "html":
        return True, match.html
    if field_type == "attribute":
        attr_name = field_spec.get("attribute")
        if not attr_name:
            return False, None
        return True, match.attributes.get(attr_name)
    if field_type in ("int", "float"):
        text = match.text(strip=True)
        if not text:
            return False, None
        try:
            return True, (int(text) if field_type == "int" else float(text))
        except (TypeError, ValueError):
            return False, None
    # Unknown type — treat as text.
    return True, match.text(strip=True) or None


def _extract_row(row_node: LexborNode, fields: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract one row dict per the field specs.

    Returns ``None`` when a non-optional field is missing AND has no
    ``default`` — that row is treated as a non-match and dropped.
    """
    out: dict[str, Any] = {}
    for spec in fields:
        name = spec.get("name")
        if not name:
            continue
        found, value = _extract_field(row_node, spec)
        if not found:
            if "default" in spec:
                out[name] = spec["default"]
            elif spec.get("optional", False):
                continue  # field absent in output
            else:
                return None  # required field missing -> drop row
        else:
            out[name] = value
    return out or None


@dataclass
class CssSchemaExtractor:
    name: str = "css"

    def extract(
        self,
        html: str,
        *,
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ExtractorResult:
        del base_url
        if not options:
            return ExtractorResult.empty(self.name)
        schema = options.get("schema") or options
        if not isinstance(schema, dict):
            return ExtractorResult.empty(self.name)

        base_selector = schema.get("baseSelector")
        fields = schema.get("fields")
        if not base_selector or not isinstance(fields, list) or not fields:
            return ExtractorResult.empty(self.name)

        if not html:
            return ExtractorResult.empty(self.name)

        parser = LexborHTMLParser(html)
        rows: list[dict[str, Any]] = []
        for node in parser.css(base_selector):
            row = _extract_row(node, fields)
            if row is not None:
                rows.append(row)

        if not rows:
            return ExtractorResult.empty(self.name)

        return ExtractorResult(
            data=rows,
            has_signal=True,
            extractor_name=self.name,
        )


def looks_like_css_schema(schema: object) -> bool:
    """Detector — returns True when ``schema`` matches the CSS shape.

    Used by ``_do_d_step`` to dispatch between LLM and CSS extraction
    when the caller passes ``schema_json``.
    """
    if not isinstance(schema, dict):
        return False
    if "baseSelector" not in schema:
        return False
    fields = schema.get("fields")
    return isinstance(fields, list) and len(fields) > 0


register(CssSchemaExtractor())
