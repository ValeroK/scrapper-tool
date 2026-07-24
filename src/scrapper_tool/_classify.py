"""Shared A/B/C/D extraction-success classifier.

Single source of truth for the accept-or-escalate decision used by BOTH
public surfaces — the REST ``/scrape`` cascade (``http_server``) and the
MCP ``auto_scrape`` cascade (``mcp``). Previously each surface inlined its
own logic and they diverged: REST accepted structured B/C/D output even
when a schema was supplied, while MCP always escalated to an LLM call in
that case. Centralising the rule keeps them behaviorally identical.

REST is the reference implementation; this function reproduces its logic
exactly, and REST delegates here without any behavior change.
"""

from __future__ import annotations

from typing import Any

from scrapper_tool._extractors.css import looks_like_css_schema

_HTTP_2XX_FLOOR = 200
_HTTP_3XX_FLOOR = 300


def classify_extraction_success(
    *,
    mode: str,
    schema_json: dict[str, Any] | None,
    force_llm_extract: bool,
    status_code: int,
    text: str,
    product: dict[str, Any] | None,
    microdata_price: dict[str, Any] | None,
    json_ld: list[Any] | None,
    css_data: list[Any] | dict[str, Any] | None = None,
) -> bool:
    """Return True when an A/B/C or Pattern-D fetch result should be accepted.

    Mirrors the historical REST ``_classify_extraction_success`` semantics:

    - ``mode == "fetch"`` always accepts (caller only wanted the page).
    - ``force_llm_extract`` with a schema forces escalation (caller wants the LLM).
    - a CSS-shaped schema accepts on CSS signal, else on any structured signal.
    - no schema accepts on a concrete B/C/CSS signal.
    - a non-CSS schema accepts when the page is readable and any signal exists.
    """
    page_readable = (_HTTP_2XX_FLOOR <= status_code < _HTTP_3XX_FLOOR) and bool(text)
    css_has_signal = bool(css_data) if css_data is not None else False
    has_any_signal = (
        product is not None or microdata_price is not None or bool(json_ld) or css_has_signal
    )

    if mode == "fetch":
        return True
    if force_llm_extract and schema_json is not None:
        return False
    if looks_like_css_schema(schema_json):
        return css_has_signal or (page_readable and has_any_signal)
    if schema_json is None:
        return product is not None or microdata_price is not None or css_has_signal
    return page_readable and has_any_signal


__all__ = ["classify_extraction_success"]
