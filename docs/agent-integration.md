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
| `agent_extract` *(v1.0.0+)* | `url, schema_json?, instruction?, model?, browser?, headful?, timeout_s?` | `AgentResult` dict (data, blocked, screenshots, actions, ...) | Render with stealth browser + 1 LLM call to extract structured JSON. Default for "scrape protected data". Requires `[llm-agent]` extra. |
| `agent_browse` *(v1.0.0+)* | `url, instruction, schema_json?, model?, browser?, max_steps?, headful?, timeout_s?` | `AgentResult` dict | Multi-step LLM-driven agent loop for interactive tasks (login, paginate, dynamic forms). Requires `[llm-agent]` extra. |

When `[llm-agent]` is not installed, the two agent tools return a `{error: "scrapper-tool[llm-agent] extra not installed", blocked: false, ...}` envelope instead of raising — the consuming agent stays operational.

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

Restart the client. The six tools appear in the tool palette. Example chat:

> *"Fetch https://example.com and tell me if any schema.org Product data is present."*

The agent will call `fetch_with_ladder` then `extract_product` and report.

For Pattern E, install with `[llm-agent]` and pass agent env vars:

```json
{
  "mcpServers": {
    "scrapper-tool": {
      "command": "scrapper-tool-mcp",
      "args": [],
      "env": {
        "SCRAPPER_TOOL_AGENT_BROWSER": "patchright",
        "SCRAPPER_TOOL_AGENT_MODEL": "qwen3-vl:8b",
        "SCRAPPER_TOOL_AGENT_OLLAMA_URL": "http://localhost:11434"
      }
    }
  }
}
```

> *"Use agent_extract on https://quotes.toscrape.com/ to get all the quotes as a JSON array."*

### Run the MCP server in Docker

The repository's `Dockerfile` produces an image with the `scrapper-tool-mcp`
entrypoint pre-baked. Two wiring patterns:

#### A) HTTP MCP — long-running Docker service (RECOMMENDED for Cursor / Claude Code remote / mcp-use)

Start the service once, then any number of MCP clients connect via URL.
Port 8000 inside the container is published to your host:

```bash
SCRAPPER_TOOL_MCP_PORT=8765 \\
SCRAPPER_TOOL_AGENT_LLM=openai_compat \\
SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:1234 \\
SCRAPPER_TOOL_AGENT_MODEL=qwen3-vl-8b-instruct \\
docker compose --profile http up -d scrapper-tool-mcp-http
```

(Replace `1234` with `6543` for the LM Studio port the user typically configures.)

Then in your client's MCP config (`.mcp.json` or equivalent), reference the URL:

```jsonc
// Cursor — Settings → MCP → Add Server, OR ~/.cursor/mcp.json
{
  "mcpServers": {
    "scrapper-tool": {
      "url": "http://localhost:8765/mcp",
      "type": "http"
    }
  }
}

// Claude Code — .mcp.json (project) or claude_desktop_config.json (global)
{
  "mcpServers": {
    "scrapper-tool": {
      "url": "http://localhost:8765/mcp"
    }
  }
}
```

Server logs:

```bash
docker logs -f scrapper-tool-mcp-http
```

Smoke-test from any Python:

```bash
SCRAPPER_TOOL_MCP_URL=http://localhost:8765/mcp \\
    uv run python scripts/e2e/test_mcp_session_http.py
```

This pattern lets one Docker service serve multiple agents and is the production
shape for remote/team deployments. The server uses MCP's **streamable-HTTP**
transport (the modern standard); fall back to `--transport sse` if your client
doesn't speak streamable-HTTP yet.

#### B) Spawn-per-call stdio (Claude Desktop's local pattern)

Each MCP request spawns a fresh container. Simpler config, slower per-call:

```json
{
  "mcpServers": {
    "scrapper-tool": {
      "command": "docker",
      "args": [
        "compose", "-f", "/abs/path/to/scrapper-tool/docker-compose.yml",
        "run", "--rm", "-T", "scrapper-tool"
      ]
    }
  }
}
```

The `-T` flag is required so docker compose doesn't allocate a pseudo-TTY
(which would corrupt the JSON-RPC stdio framing). Note: on Windows, the host
side of this pipe occasionally has issues with the `mcp` Python SDK — pattern
A above is more robust cross-platform.

#### Bring-your-own LLM

The image does NOT bundle an LLM — it expects an external Ollama / LM Studio /
llama.cpp / vLLM server reachable at `host.docker.internal`. Default
`SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:11434`. Override in
`.env` next to `docker-compose.yml` to point elsewhere — see
[README → External LLMs](../README.md#external-llms-lm-studio-llamacpp-vllm-remote-ollama)
for the table of `LLM` / `URL` pairs.

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

If step 1 returns `blocked: true` (all four profiles 403'd), the agent escalates to **Pattern E** by calling `agent_extract` (default for "scrape this data") or `agent_browse` (interactive multi-step tasks). Both are MCP-exposed. See [Pattern E docs](patterns/e-llm-agent.md) for the full surface.

## Security

The MCP server runs in **the agent's trust boundary**, not the user's. Two implications:

1. **The agent is responsible for confirming user-data-bearing fetches**. `fetch_with_ladder` (and the Pattern E tools) will happily fetch any URL given. The consuming agent's permission model (Claude's tool-use approval, OpenClaw's plugin gating, etc.) must prompt the user before fetching URLs that could leak personal data.
2. **The lib does not bundle authentication**. If a fetch needs cookies / OAuth tokens, the agent passes them through Pattern E's headful browser flow (login interactively) or wires them via `extra_headers` on the underlying `httpx` client. Don't tunnel secrets through the URL.

Body truncation: responses over 64 KB are truncated server-side so a single fetch can't blow the agent's context window. The `truncated: true` flag in the response signals when this happened — the agent can re-fetch a narrower URL or paginate.

## Versioning

- **v0.2.0** — `fetch_with_ladder`, `extract_product`, `extract_microdata_price`, `canary`. Stdio transport.
- **v1.0.0** (current) — adds `agent_extract` and `agent_browse` (Pattern E); also adds streamable-HTTP and SSE transports alongside stdio. Tool surface frozen under SemVer.
- **v1.1.0** (planned) — pluggable rate-limit / robots.txt policies; per-vendor profile presets; `agent_session()` warm-browser pooling.

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
