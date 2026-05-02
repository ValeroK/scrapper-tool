# Quickstart

Get from zero to a working scrape in under 5 minutes.

---

## Pick your interface

scrapper-tool exposes the same A-E capability stack through three surfaces:

| You are... | Use this | Doc |
|---|---|---|
| Building a Python service | The Python SDK | this page |
| Building a non-Python service (Node, Go, PHP, ...) | The HTTP REST sidecar | [`http-sidecar.md`](http-sidecar.md) |
| Building an LLM agent (Claude Code, Cursor, ...) | The MCP server | [`agent-integration.md`](agent-integration.md) |

The rest of this page covers the SDK. The other two surfaces have stupid-proof quick starts in their respective docs.

---

## Install

Pick the install matrix that matches what you need:

```bash
# Just patterns A/B/C (lightweight, no browser, no LLM)
pip install scrapper-tool

# + Pattern D (Scrapling stealth browser)
pip install 'scrapper-tool[hostile]'

# + Pattern E (LLM-driven scraping for any protected site)
pip install 'scrapper-tool[llm-agent]'

# Everything in one environment (recommended for "ultimate scraper")
pip install 'scrapper-tool[full,agent]'

# Plus the REST sidecar (FastAPI + uvicorn)
pip install 'scrapper-tool[full,agent,http]'
```

Or with `uv` (faster):
```bash
uv add 'scrapper-tool[full,agent]'
```

---

## Pattern A — fetch any page

The TLS-impersonation ladder walks four Chrome / Safari / Firefox profiles until one returns non-403/503.

```python
import asyncio
from scrapper_tool import request_with_ladder

async def main():
    response, profile = await request_with_ladder("GET", "https://example.com")
    print(f"Status: {response.status_code}, won with: {profile}")
    print(response.text[:500])

asyncio.run(main())
```

Raises `BlockedError` if all four profiles return 403 — escalate to Pattern D or E.

---

## Pattern B — structured product data (JSON-LD / microdata)

Most modern e-commerce sites embed schema.org `Product` blocks as JSON-LD. Pattern B parses them into a typed `ProductOffer` dict.

```python
from scrapper_tool.patterns.b import extract_product_offer

product = extract_product_offer(html, base_url="https://target.com/p/123")
if product:
    print(product.name, product.price, product.currency)
    # 'Widget Pro X', Decimal('29.99'), 'USD'
```

Returns `None` when there's no Product block on the page.

---

## Pattern C — price from microdata

For sites that use `<meta itemprop="price">` instead of JSON-LD:

```python
from scrapper_tool.patterns.c import extract_microdata_price

result = extract_microdata_price(html)
if result:
    price, currency = result
    print(f"{price} {currency}")
```

---

## Auto-escalating scrape (the easy mode)

Want structured data and don't care which pattern produces it? Use the HTTP sidecar's `/scrape` endpoint or build the same flow yourself:

```python
import asyncio
from scrapper_tool import request_with_ladder, BlockedError
from scrapper_tool.patterns.b import extract_product_offer
from scrapper_tool.agent import agent_extract  # requires [llm-agent]

async def scrape_anything(url: str, schema: dict | None = None):
    # Try Pattern A/B/C first
    try:
        response, _profile = await request_with_ladder("GET", url)
        product = extract_product_offer(response.text, base_url=str(response.url))
        if product is not None:
            return {"pattern_used": "a_b_c", "product": product.model_dump()}
    except BlockedError:
        pass

    # Escalate to Pattern E1
    result = await agent_extract(url, schema or {"type": "object"})
    return {"pattern_used": "e1", "data": result.data}

asyncio.run(scrape_anything("https://target.com/product/123"))
```

The HTTP sidecar's `/scrape` endpoint does this exact ladder server-side — see [`http-sidecar.md`](http-sidecar.md).

---

## Pattern E — LLM agent for protected sites

When stealth fingerprinting alone isn't enough, Pattern E renders the page with a stealth browser and uses a local LLM to extract structured data.

```python
import asyncio
from scrapper_tool.agent import agent_extract

async def main():
    result = await agent_extract(
        "https://protected.com/product/123",
        schema={"name": "str", "price": "float", "in_stock": "bool"},
        model="qwen3-vl:8b",  # any Ollama / LM Studio / vLLM model
    )
    print(result.data)        # {"name": "...", "price": 29.99, "in_stock": True}
    print(result.tokens_used) # ~1500 tokens for a typical page

asyncio.run(main())
```

For interactive flows (login, pagination, dynamic forms) use `agent_browse` instead — see [`patterns/e-llm-agent.md`](patterns/e-llm-agent.md).

---

## Testing with fixtures

Your scrapers should use deterministic fixture-replay tests, not live network calls. The [`scrapper_tool.testing`](reference/testing.md) module ships `FakeCurlSession` for exactly this:

```python
from scrapper_tool.testing import FakeCurlSession

def test_my_adapter():
    FakeCurlSession.reset()
    FakeCurlSession.STATUS_FOR_PROFILE = {"chrome133a": 200}
    FakeCurlSession.RESPONSE_TEXT_FOR_PROFILE = {"chrome133a": "<html>...</html>"}
    # ...your adapter test here, with monkeypatch.setattr...
```

See [`reference/testing.md`](reference/testing.md) for the full pattern and [`tests/unit/`](../tests/unit/) for real examples.

---

## Where to go next

- **Service-to-service integration** → [`http-sidecar.md`](http-sidecar.md)
- **LLM agent / Claude Code wiring** → [`agent-integration.md`](agent-integration.md)
- **All env vars and where they go** → [`SETTINGS.md`](SETTINGS.md)
- **Pattern E deep dive** → [`patterns/e-llm-agent.md`](patterns/e-llm-agent.md)
- **Reverse-engineering a new vendor site** → [`recon.md`](recon.md)
