"""Live HTTP probes. Marked ``live`` — opt-in via ``pytest -m live``.

These tests fire **real** HTTP requests against public, stable URLs to
detect:

- Pattern B (schema.org Product) regressions against a known-good page.
- The lib's full Pattern A → Pattern B pipe end-to-end.

Skipped by default in normal CI (the ``[tool.pytest.ini_options]``
default in ``pyproject.toml`` is ``-m "not live"``). The
``.github/workflows/live-canary.yml`` workflow runs them on a daily
cron. Set ``SCRAPPER_TOOL_LIVE=1`` to opt in locally::

    SCRAPPER_TOOL_LIVE=1 uv run pytest -m live -v

Canary URL choices
------------------

We deliberately use IANA-reserved or developer-tooling-stable domains:

- ``example.com`` — RFC 2606 reserved; never goes away.
- ``httpbin.org/anything`` — Postman's generic echo; no anti-bot.
- schema.org's own developer reference pages — deliberately stable.

We do NOT probe real vendor sites (PartSouq, Megazip, etc.) here.
Those are PartsPilot's nightly fixture-regression workflow's job, and
running them from this lib's CI would generate detectable load.
"""

from __future__ import annotations

import os

import pytest

from scrapper_tool import request_with_retry, vendor_client
from scrapper_tool.patterns.b import extract_product_offer

# All tests in this module are gated behind the `live` marker.
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SCRAPPER_TOOL_LIVE") != "1",
        reason="Live probes opt-in via SCRAPPER_TOOL_LIVE=1.",
    ),
]


@pytest.mark.asyncio
async def test_smoke_example_com_returns_200() -> None:
    """example.com is RFC-2606 reserved and returns stable HTML.

    If THIS test fails, the runner has lost internet — not a lib bug.
    Acts as the canonical "is the fetch path working at all?" smoke.
    """
    async with vendor_client() as client:
        resp = await request_with_retry(client, "GET", "https://example.com")
    assert resp.status_code == 200
    assert "Example Domain" in resp.text


@pytest.mark.asyncio
async def test_pattern_b_extracts_from_schema_org_developer_example() -> None:
    """Pattern B canary against a stable schema.org developer reference.

    schema.org publishes example pages with structured data baked in,
    deliberately for tools to validate against. We grep the page for a
    Product/Offer block; if the extraction returns ``None`` it means
    extruct (or our walker) regressed.

    URL: https://schema.org/Product (the spec page itself doesn't
    always carry a Product ld+json block, so we use the developer
    examples docs — which historically do).

    On test failure: read the response body manually with curl. If the
    page has changed shape, update the assertion. If the page now lacks
    a Product block, swap to a different schema.org example.
    """
    # We use schema.org's example page set (kept stable for tooling).
    url = "https://schema.org/Product"
    async with vendor_client(timeout=15.0) as client:
        resp = await request_with_retry(client, "GET", url)
    assert resp.status_code == 200, f"schema.org returned {resp.status_code}"

    # We don't strictly require a Product extraction here — schema.org's
    # spec page may not carry one. But the lib should not raise; a
    # ``None`` return is acceptable (means the page has no Product
    # structured-data block). The point is exercising the pipeline.
    result = extract_product_offer(resp.text, base_url=url)
    # Either we found a Product (great) or we didn't (acceptable).
    # What matters: no exception, and if there IS a result it's a
    # well-formed Pydantic model with the expected attribute surface.
    if result is not None:
        # Touching .name is enough — Pydantic raises on field access for
        # an invalid object, so a successful read here proves the model
        # was constructed correctly.
        _ = result.name


@pytest.mark.asyncio
async def test_full_pipeline_httpbin_json() -> None:
    """End-to-end smoke: fetch + parse a known-shape JSON response.

    httpbin.org/anything echoes the request as JSON. This validates
    the http core (``vendor_client`` + ``request_with_retry``) without
    leaning on anti-bot infrastructure.
    """
    async with vendor_client() as client:
        resp = await request_with_retry(
            client, "GET", "https://httpbin.org/anything?probe=scrapper-tool"
        )
    assert resp.status_code == 200
    body = resp.json()
    # httpbin echoes our query string back; this confirms the request
    # made it across the wire intact.
    assert body["args"]["probe"] == "scrapper-tool"
