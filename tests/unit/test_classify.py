"""Branch-table tests for the shared extraction-success classifier.

This is the REST ``/scrape`` accept rule, extracted verbatim into
``scrapper_tool._classify`` so the MCP ``auto_scrape`` cascade can share
it. The table below is the behavioral snapshot — if REST semantics ever
change, these assertions must change with them (deliberately).
"""

from __future__ import annotations

import pytest

from scrapper_tool._classify import classify_extraction_success

_PRODUCT = {"name": "x"}
_PRICE = {"price": "1.0", "currency": "USD"}
_CSS_SCHEMA = {"baseSelector": ".p", "fields": [{"name": "t", "selector": "h1"}]}
_OBJ_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}}


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # fetch mode always accepts, regardless of signal.
        ({"mode": "fetch", "schema_json": None, "status_code": 500, "text": ""}, True),
        # force_llm + schema always escalates (rejects).
        (
            {
                "mode": "auto",
                "schema_json": _OBJ_SCHEMA,
                "force_llm_extract": True,
                "product": _PRODUCT,
            },
            False,
        ),
        # No schema, has product -> accept.
        ({"mode": "auto", "schema_json": None, "product": _PRODUCT}, True),
        # No schema, has price -> accept.
        ({"mode": "auto", "schema_json": None, "microdata_price": _PRICE}, True),
        # No schema, no signal -> reject.
        ({"mode": "auto", "schema_json": None}, False),
        # CSS schema with CSS signal -> accept.
        ({"mode": "auto", "schema_json": _CSS_SCHEMA, "css_data": [{"t": "a"}]}, True),
        # CSS schema, no CSS but B/C signal + readable -> accept.
        (
            {
                "mode": "auto",
                "schema_json": _CSS_SCHEMA,
                "product": _PRODUCT,
                "status_code": 200,
                "text": "x",
            },
            True,
        ),
        # Non-CSS schema, readable + signal -> accept (the MCP-divergence fix).
        (
            {
                "mode": "auto",
                "schema_json": _OBJ_SCHEMA,
                "product": _PRODUCT,
                "status_code": 200,
                "text": "x",
            },
            True,
        ),
        # Non-CSS schema, signal but NOT readable (bad status) -> reject.
        (
            {
                "mode": "auto",
                "schema_json": _OBJ_SCHEMA,
                "product": _PRODUCT,
                "status_code": 403,
                "text": "",
            },
            False,
        ),
        # Non-CSS schema, readable but no signal -> reject.
        ({"mode": "auto", "schema_json": _OBJ_SCHEMA, "status_code": 200, "text": "x"}, False),
    ],
)
def test_classify_branch_table(kwargs: dict, expected: bool) -> None:
    base = {
        "force_llm_extract": False,
        "status_code": 200,
        "text": "body",
        "product": None,
        "microdata_price": None,
        "json_ld": None,
        "css_data": None,
    }
    base.update(kwargs)
    assert classify_extraction_success(**base) is expected


def test_json_ld_alone_is_a_signal() -> None:
    # A raw json_ld block counts even without a parsed product.
    assert (
        classify_extraction_success(
            mode="auto",
            schema_json=_OBJ_SCHEMA,
            force_llm_extract=False,
            status_code=200,
            text="body",
            product=None,
            microdata_price=None,
            json_ld=[{"@type": "Thing"}],
        )
        is True
    )
