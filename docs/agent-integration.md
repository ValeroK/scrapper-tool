# Agent integration

> **Status: stub for v0.0.x. The MCP server lands in v0.2.0 (M13).** This doc is reserved at the top-level docs IA so the slot is visible from M0 — no retrofitting required when M13 ships.

## What this will cover

`scrapper-tool` ships an optional MCP server (`pip install scrapper-tool[agent]`) that exposes the lib's helpers as tools any [Model Context Protocol](https://modelcontextprotocol.io)-compatible agent can call:

- `fetch_with_ladder` — issue an HTTP request through the four-profile impersonation ladder; report which profile won.
- `extract_product` — parse a schema.org Product+Offer block from HTML (Pattern B).
- `extract_microdata_price` — parse `itemprop="price"` schema.org microdata from HTML (Pattern C).
- `recon_classify` — classify a URL into Pattern A/B/C/D programmatically.
- `hostile_fetch` — Scrapling-backed fetch with auto-Turnstile-solve (requires `[hostile]` extra).
- `canary` — fire one probe per impersonation profile against a URL, report 200/403/timeout per profile.

Sections planned for M13:

1. **MCP via Claude Code / Claude Desktop** — `.mcp.json` snippet.
2. **MCP via the Anthropic SDK + `mcp-use`** — Python snippet.
3. **OpenClaw integration** — manifest at `docs/agent-integration/openclaw.json`.
4. **Hermes Agent integration** — manifest at `docs/agent-integration/hermes.yaml`.
5. **AutoGen / LangChain via `mcp-use`** — code snippets.
6. **Security note** — the MCP server runs in the user's trust boundary; tool docstrings call out unsafe inputs (raw URLs from untrusted content); the consuming agent's own permission model gates user-data-bearing fetches.

## Why MCP

The 2026 agent stacks (Hermes Agent's plugin system, OpenClaw's tool registry, Composio connectors) all consume MCP servers. Reference implementations — Microsoft's [Playwright MCP](https://playwright.dev/python/agents) (~29k stars), Browserbase's Stagehand MCP (~21k stars), MaitreyaM's Crawl4AI MCP, luminati-io's web-scraping-with-mcp — all converge on the same shape.

The official Python MCP SDK (`mcp` package) is the canonical path; `mcp-use` bridges it to LangChain/AutoGen/the Anthropic SDK directly.

---

*Detailed integration docs land in v0.2.0 (M13). Track [#TBD](https://github.com/ValeroK/scrapper-tool/issues) for the milestone.*
