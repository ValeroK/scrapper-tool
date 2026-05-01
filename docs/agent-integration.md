# Agent integration — MCP server

`scrapper-tool` ships an optional MCP server (Model Context Protocol) that exposes the lib's helpers as tools any LLM agent can call. Available since **v0.2.0** (M13). Install:

```bash
pip install scrapper-tool[agent]
```

Then start the stdio server:

```bash
scrapper-tool-mcp
```

This is a stdio MCP server compatible with **Claude Desktop**, **Claude Code**, **OpenClaw**, **Hermes Agent**, and any other MCP-aware client.

## Tools exposed

| Tool | Input | Output | Use when |
|---|---|---|---|
| `fetch_with_ladder` | `url, method?, use_curl_cffi?` | `{status, body (≤64 KB), winning_profile, blocked, error}` | Agent needs to fetch a URL that may TLS-fingerprint |
| `extract_product` | `html, base_url?` | `ProductOffer` dict or `null` | Agent has HTML and wants schema.org Product+Offer fields |
| `extract_microdata_price` | `html` | `{price, currency}` or `null` | Agent has HTML with `<meta itemprop="price">` anchors |
| `canary` | `url, profiles?` | Per-profile probe results | Agent diagnosing which TLS fingerprint a site rejects |

## Wiring it up

### Claude Code / Claude Desktop

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "scrapper-tool": {
      "command": "scrapper-tool-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

Restart the client. The four tools appear in the tool palette. Example chat:

> *"Fetch https://example.com and tell me if any schema.org Product data is present."*

The agent will call `fetch_with_ladder` then `extract_product` and report.

### Anthropic Python SDK + `mcp-use`

```python
import asyncio
from mcp_use import MCPClient
from anthropic import Anthropic

async def main() -> None:
    client = MCPClient.from_dict({
        "mcpServers": {
            "scrapper-tool": {"command": "scrapper-tool-mcp"},
        }
    })
    async with client:
        tools = await client.list_tools()
        anthropic = Anthropic()
        msg = anthropic.messages.create(
            model="claude-opus-4-5",
            max_tokens=2048,
            tools=tools,
            messages=[{"role": "user", "content": "Probe httpbin.org/anything"}],
        )
        print(msg)

asyncio.run(main())
```

### OpenClaw / Hermes Agent

Both consume MCP servers natively. Point them at the `scrapper-tool-mcp` command (same as Claude Desktop). See [Composio's Hermes integration patterns](https://composio.dev/toolkits/scrapingbee/framework/hermes-agent) for reference wiring.

### AutoGen / LangChain

Both have first-class MCP support via [`mcp-use`](https://mcp-use.com/docs/python/integration/anthropic). Same `MCPClient` invocation as the Anthropic SDK example above — the resulting `tools` list plugs into either framework's tool registration.

## Example session

User asks the agent: *"Get me the price of [some product URL]."*

The agent's flow:

1. Calls `fetch_with_ladder(url=...)`. Server walks `chrome133a → chrome124 → safari → firefox` until a profile returns 200. Returns the HTML body (truncated to 64 KB).
2. Calls `extract_product(html=<body>, base_url=<url>)`. Server returns a normalised `ProductOffer` dict.
3. Reports `{name}: {price} {currency}` to the user.

If step 1 returns `blocked: true` (all four profiles 403'd), the agent knows the site needs Pattern D and can either escalate to a `hostile_fetch` (not yet exposed in v0.2.0; planned for v0.3.0) or report the block to the user.

## Security

The MCP server runs in **the agent's trust boundary**, not the user's. Two implications:

1. **The agent is responsible for confirming user-data-bearing fetches**. `fetch_with_ladder` will happily fetch any URL it's given. The consuming agent's permission model (Claude's tool-use approval, OpenClaw's plugin gating, etc.) must prompt the user before fetching URLs that could leak personal data.
2. **The lib does not bundle authentication**. If a fetch needs cookies / OAuth tokens, the agent passes them as `extra_headers` (not yet exposed in v0.2.0; planned for v0.3.0). Don't tunnel secrets through the URL.

Body truncation: responses over 64 KB are truncated server-side so a single fetch can't blow the agent's context window. The `truncated: true` flag in the response signals when this happened — the agent can re-fetch a narrower URL or paginate.

## Versioning

- **v0.2.0** (this release) — `fetch_with_ladder`, `extract_product`, `extract_microdata_price`, `canary`. Stdio transport.
- **v0.3.0** (planned) — `hostile_fetch` (Scrapling-backed Pattern D), `recon_classify` (auto-decide which pattern fits a URL). HTTP/SSE transport for hosted-agent platforms.
- **v1.0.0** — API stability commitment; tool surface frozen.

The lib's [`CONTRIBUTING.md`](../CONTRIBUTING.md#quarterly-review-checklist) carries the maintenance contract — quarterly review of the MCP SDK pin and the tool catalogue.

## References

- Model Context Protocol — https://modelcontextprotocol.io
- Official Python SDK — https://github.com/modelcontextprotocol/python-sdk
- `mcp-use` (LangChain/AutoGen/Anthropic-SDK bridge) — https://mcp-use.com
- Playwright MCP (Microsoft, ~29k stars; reference for browser-based MCP servers) — https://playwright.dev/python/agents
- Stagehand MCP (Browserbase) — https://www.morphllm.com/stagehand-mcp
- Crawl4AI MCP server — https://github.com/MaitreyaM/WEB-SCRAPING-MCP
- Bright Data web-scraping-with-mcp — https://github.com/luminati-io/web-scraping-with-mcp
- OpenClaw vs Hermes Agent — https://petronellatech.com/blog/openclaw-vs-hermes-agent-2026/
