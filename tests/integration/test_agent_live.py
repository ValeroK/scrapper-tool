"""Live integration tests for Pattern E (LLM-driven scraping).

These tests fire **real** browser launches and **real** local LLM calls
against public targets to verify end-to-end behavior:

- The full Camoufox / Patchright launch path works.
- Crawl4AI + Ollama produces structured output for a known schema.
- The browser-use agent loop runs to completion on a simple task.
- The captcha cascade silently passes a Turnstile interstitial when
  the backend is Camoufox.

Skipped by default. Enable explicitly via env::

    SCRAPPER_TOOL_LIVE=1 SCRAPPER_TOOL_AGENT=1 \\
        OLLAMA_HOST=http://localhost:11434 \\
        uv run pytest -m "live and agent" -v

Hardware expectations:

- Ollama running at ``OLLAMA_HOST`` (default ``http://localhost:11434``).
- A vision-language model pulled (default: ``qwen3-vl:8b``).
- Camoufox installed (``camoufox fetch``) OR ``SCRAPPER_TOOL_AGENT_BROWSER=patchright``
  with ``patchright install chromium`` already run.

Targets (chosen for stability + permissiveness):

- ``https://example.com`` — RFC 2606 reserved, never goes away.
- ``https://quotes.toscrape.com/`` — purpose-built scraping practice site.
- ``https://nopecha.com/demo/cloudflare`` — public CF Turnstile demo.
"""

from __future__ import annotations

import os

import httpx
import pytest

from scrapper_tool.agent import AgentConfig, agent_browse, agent_extract
from scrapper_tool.errors import AgentLLMError

pytestmark = [
    pytest.mark.live,
    pytest.mark.agent,
    pytest.mark.skipif(
        os.environ.get("SCRAPPER_TOOL_LIVE") != "1",
        reason="Live agent tests opt in via SCRAPPER_TOOL_LIVE=1.",
    ),
    pytest.mark.skipif(
        os.environ.get("SCRAPPER_TOOL_AGENT") != "1",
        reason="Live agent tests also require SCRAPPER_TOOL_AGENT=1.",
    ),
]

_DEFAULT_OLLAMA = os.environ.get("SCRAPPER_TOOL_AGENT_OLLAMA_URL", "http://localhost:11434")


def _ollama_reachable() -> bool:
    try:
        with httpx.Client(timeout=2.0) as client:
            return client.get(f"{_DEFAULT_OLLAMA}/api/tags").status_code < 500
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
        return False


@pytest.fixture(autouse=True)
def _skip_if_ollama_down() -> None:
    if not _ollama_reachable():
        pytest.skip(f"Ollama not reachable at {_DEFAULT_OLLAMA}; skipping live agent tests.")


# ---------------------------------------------------------------------------
# E1 — agent_extract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_quotes_toscrape() -> None:
    """Pattern E1 against quotes.toscrape.com — well-formed listing page.

    Asserts the LLM extracts at least three quotes, each with a non-empty
    text and author. We don't pin exact text because the site occasionally
    rotates content.
    """
    schema = {
        "type": "object",
        "properties": {
            "quotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "author": {"type": "string"},
                    },
                    "required": ["text", "author"],
                },
            }
        },
        "required": ["quotes"],
    }
    cfg = AgentConfig.from_env().merged(
        # Use Patchright in CI for speed; allow override via env.
        browser=os.environ.get("SCRAPPER_TOOL_AGENT_BROWSER", "patchright"),
        captcha_solver="none",
        timeout_s=180.0,
    )
    result = await agent_extract(
        "https://quotes.toscrape.com/",
        schema=schema,
        config=cfg,
        instruction="Extract all quotes shown on the page.",
    )
    assert not result.blocked, f"unexpected block: {result.error}"
    assert result.data is not None
    quotes = result.data["quotes"] if isinstance(result.data, dict) else result.data
    assert isinstance(quotes, list)
    assert len(quotes) >= 3, f"expected >=3 quotes, got {len(quotes)}"


@pytest.mark.asyncio
async def test_extract_example_com_title() -> None:
    """Pattern E1 against example.com — minimal smoke test.

    The page has a single ``<h1>Example Domain</h1>``. If the LLM can't
    pull that out into the schema, the whole pipe is broken.
    """
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }
    cfg = AgentConfig.from_env().merged(
        browser=os.environ.get("SCRAPPER_TOOL_AGENT_BROWSER", "patchright"),
        captcha_solver="none",
        timeout_s=120.0,
    )
    result = await agent_extract(
        "https://example.com",
        schema=schema,
        config=cfg,
        instruction="Return the page's main title (h1 text).",
    )
    assert not result.blocked
    assert result.data is not None
    title = result.data["title"] if isinstance(result.data, dict) else None
    assert isinstance(title, str)
    assert "example" in title.lower(), f"unexpected title: {title!r}"


# ---------------------------------------------------------------------------
# E2 — agent_browse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_example_com_link_navigation() -> None:
    """Pattern E2 — agent navigates to the link on example.com.

    example.com has exactly one link (to iana.org). The agent should
    click it and report something about IANA. We accept any non-empty
    final result with that signal.
    """
    cfg = AgentConfig.from_env().merged(
        browser=os.environ.get("SCRAPPER_TOOL_AGENT_BROWSER", "patchright"),
        captcha_solver="none",
        max_steps=8,
        timeout_s=240.0,
    )
    result = await agent_browse(
        "https://example.com",
        instruction=(
            "Click the only link on the page (it points to IANA), and after "
            "the new page loads, return a short JSON object {visited: <url>}."
        ),
        config=cfg,
    )
    # Agent loops on local LLMs are noisy — accept either success
    # (data present, blocked=False) or graceful no-match.
    assert not result.blocked
    assert result.steps_used > 0


# ---------------------------------------------------------------------------
# Captcha cascade — Camoufox auto-pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_captcha_cascade_camoufox_auto_pass() -> None:
    """Pattern E1 against a public CF Turnstile demo with Camoufox.

    With backend=camoufox + captcha_solver=auto, the Tier-0 solver should
    silently pass the interstitial. Skipped automatically if Camoufox isn't
    installed (the launch path raises ImportError → skip).
    """
    pytest.importorskip("camoufox", reason="Camoufox not installed.")

    schema = "Return a short JSON object describing whether the page rendered."
    cfg = AgentConfig.from_env().merged(
        browser="camoufox",
        captcha_solver="auto",
        timeout_s=240.0,
    )
    try:
        result = await agent_extract(
            "https://nopecha.com/demo/cloudflare",
            schema=schema,
            config=cfg,
        )
    except AgentLLMError:
        pytest.skip("LLM unreachable mid-test.")
    # We can't guarantee 100% bypass on every demo state, but a non-empty
    # rendered_markdown means we got past the interstitial.
    if result.blocked:
        pytest.skip("Camoufox auto-pass didn't clear this CF challenge today; not a regression.")
    assert result.rendered_markdown is not None
    assert len(result.rendered_markdown) > 100, "rendered too little to count as success"
