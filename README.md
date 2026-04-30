# scrapper-tool

[![CI](https://github.com/ValeroK/scrapper-tool/actions/workflows/ci.yml/badge.svg)](https://github.com/ValeroK/scrapper-tool/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/scrapper-tool.svg)](https://pypi.org/project/scrapper-tool/)
[![Python versions](https://img.shields.io/pypi/pyversions/scrapper-tool.svg)](https://pypi.org/project/scrapper-tool/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A reusable Python web-scraping toolkit. Built from the production scraping primitives behind [PartsPilot](https://github.com/ValeroK/affiliate-service), extracted as an open-source library so other projects (and LLM agents) can pick up the same patterns without redoing the reverse-engineering work.

> **Status (2026-04-30):** alpha. v0.1.0 covers the core pattern ladder, anti-bot helpers, and deterministic fixture-replay testing. v0.2.0 adds an MCP server for LLM agents (Claude, OpenClaw, Hermes Agent, AutoGen, LangChain).

## What it solves

Web scraping in 2026 is dominated by four recurring patterns. This lib gives each pattern a documented helper plus the surrounding infrastructure (HTTP client with TLS-impersonation fallback, retry/backoff, fixture-replay testing) so you don't reinvent them per vendor:

| Pattern | When | Helper | Cost |
|---|---|---|---|
| **A — JSON API** | DevTools shows an XHR returning the price-bearing JSON. Anonymous or OAuth. | `vendor_client()` + your own response model | Lowest — parse, validate, done. |
| **B — Embedded JSON** | Document HTML carries `<script type="application/ld+json">`, `__NEXT_DATA__`, `__NUXT__`, or `self.__next_f.push(...)`. | `patterns.b.extract_product_offer()` (via [`extruct`](https://github.com/scrapinghub/extruct)) | Low — one call, broad markup coverage. |
| **C — CSS / microdata** | Price visible in HTML, no embedded JSON. Prefer `itemprop="price"` schema.org microdata. | `patterns.c.extract_microdata_price()` (via [`selectolax`](https://github.com/rushter/selectolax)) | Medium — selectors break on ancestor reshuffles. |
| **D — Hostile** | Cloudflare Turnstile, Akamai EVA, etc. defeat both default `httpx` and `curl_cffi`. | `patterns.d.hostile_client()` (via [Scrapling](https://github.com/D4Vinci/Scrapling)) — `pip install scrapper-tool[hostile]` | Highest — Playwright runtime, ≈400 MB image bloat. |

Plus a four-profile **anti-bot ladder** (`chrome133a → chrome124 → safari18_0 → firefox135`) that auto-walks when a profile gets fingerprinted, and a `scrapper-tool canary` CLI for nightly fingerprint-health probes.

## Install

```bash
pip install scrapper-tool                # core: httpx + curl_cffi + selectolax + extruct
pip install scrapper-tool[hostile]       # adds Scrapling for Cloudflare Turnstile
pip install scrapper-tool[agent]         # adds the MCP server (v0.2.0+) for LLM agents
```

## Quickstart (5 minutes)

```python
import asyncio
from scrapper_tool import vendor_client, request_with_retry
from scrapper_tool.patterns.b import extract_product_offer

async def main() -> None:
    async with vendor_client() as client:
        resp = await request_with_retry(client, "GET", "https://example-shop.test/product/123")
        product = extract_product_offer(resp.text, base_url=str(resp.url))
        print(product)

asyncio.run(main())
```

For TLS-sensitive vendors, flip one switch:

```python
async with vendor_client(use_curl_cffi=True) as client:
    ...   # walks chrome133a → chrome124 → safari → firefox until one returns 200
```

## Documentation

- **[Quickstart](docs/quickstart.md)** — 5-minute on-ramp.
- **[Recon playbook](docs/recon.md)** — DevTools-driven reverse-engineering of a new vendor site.
- **[Pattern A](docs/patterns/a-json-api.md)** / **[B](docs/patterns/b-embedded-json.md)** / **[C](docs/patterns/c-css-microdata.md)** / **[D](docs/patterns/d-hostile.md)**
- **[Anti-bot ladder reference](docs/reference/ladder.md)** — how the ladder walks, when to bump the primary profile.
- **[Test helpers](docs/reference/testing.md)** — `FakeCurlSession`, `replay_fixture`, golden-snapshot pattern.
- **[Agent integration](docs/agent-integration.md)** — MCP wiring for Claude, OpenClaw, Hermes Agent, AutoGen, LangChain. *(v0.2.0+)*
- **[2026-04-30 landscape research](docs/research/2026-04-30-landscape.md)** — why these tools, sourced.

## Why these tools?

See **[`docs/research/2026-04-30-landscape.md`](docs/research/2026-04-30-landscape.md)** for the sourced rationale. Short version: `curl_cffi` is the only actively-maintained TLS-impersonation lib with chrome131+/chrome133a/chrome142/chrome146 profiles; `puppeteer-stealth` and `playwright-extra` were deprecated in 2025-02; Scrapling is the only OSS Playwright-based stack with a working Turnstile auto-solve as of 2026; managed SaaS (Firecrawl, ZenRows, Bright Data) is deliberately not bundled.

## Contributing

This is a living document — every PR that meaningfully changes how we scrape lands a `CHANGELOG.md` row. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the maintenance contract.

## License

MIT — see [`LICENSE`](LICENSE).
