"""Live checks for the stealth-browser render tier. Opt-in via ``pytest -m live``.

Fires a **real** browser at real URLs, so it's gated twice: the ``live`` marker
(excluded by the default ``-m "not live"``) plus ``SCRAPPER_TOOL_LIVE=1``. It also
needs a stealth browser installed (``camoufox fetch``), so it skips cleanly when
the ``[llm-agent]`` extra / browser binary is missing::

    SCRAPPER_TOOL_LIVE=1 uv run pytest -m live tests/integration/test_render_live.py -v

Target choice: ``quotes.toscrape.com/js/`` is a purpose-built scraping sandbox whose
content is rendered **by JavaScript only** — a plain HTTP fetch returns an empty
list. That makes it the honest proof that the render tier really executes JS,
without generating load on a production vendor site.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SCRAPPER_TOOL_LIVE") != "1",
        reason="Live render probes opt-in via SCRAPPER_TOOL_LIVE=1.",
    ),
]

_JS_SANDBOX = "https://quotes.toscrape.com/js/"


@pytest.mark.asyncio
async def test_render_executes_javascript() -> None:
    """The render tier must return DOM that only exists after JS runs."""
    pytest.importorskip("camoufox", reason="needs the [llm-agent] extra")
    from scrapper_tool.patterns.render import render_html

    result = await render_html(_JS_SANDBOX, browser="camoufox", settle_s=2.0, timeout_s=60)

    assert result.status == 200
    assert result.final_url.startswith("https://quotes.toscrape.com")
    # The quote blocks are injected by JS — their presence proves execution.
    assert result.html.count('class="quote"') >= 5, "JS-rendered content missing"


@pytest.mark.asyncio
async def test_rendered_html_feeds_the_extractors() -> None:
    """Rendered HTML must be consumable by the existing deterministic extractors.

    This is the whole point of the tier: render once, then parse with Pattern
    B/C/CSS instead of paying for an LLM.
    """
    pytest.importorskip("camoufox", reason="needs the [llm-agent] extra")
    from scrapper_tool._extractors import get as get_extractor
    from scrapper_tool.patterns.render import render_html

    result = await render_html(_JS_SANDBOX, browser="camoufox", settle_s=2.0, timeout_s=60)

    # The extractor must run cleanly on the rendered DOM. quotes.toscrape has no
    # schema.org Product, so has_signal is legitimately False — we're asserting
    # the plumbing works, not that this page has products.
    outcome = get_extractor("json_ld_product").extract(result.html, base_url=result.final_url)
    assert outcome.extractor_name == "json_ld_product"
    assert outcome.has_signal is False
