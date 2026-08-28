<!-- The wiring and tool-surface half of this page was moved out of README.md
on 2026-08-27, verbatim, when the README was trimmed to a front door. The
Docker-entrypoint half below predates that. -->

# Running scrapper-tool as an MCP server

`scrapper-tool` ships an MCP server that exposes every pattern as a tool any
MCP-aware client (Claude Desktop, Claude Code, OpenClaw, Hermes Agent, AutoGen,
LangChain) can call.

> **Teaching an agent to use it:** [`skills/scrapper-tool/SKILL.md`](../skills/scrapper-tool/SKILL.md)
> is a portable Agent Skill that gives an LLM the know-how to drive scrapper-tool
> — which entrypoint to call, how the cascade escalates, and how to read the
> result. Load it as a Claude skill, a Cursor rule, or plain context; see
> [`skills/README.md`](../skills/README.md). Pair it with the MCP server (know-how +
> capability).

#### Tools exposed

| Tool | Purpose |
|------|---------|
| `auto_scrape(url, schema_json, instruction, model, browser, timeout_s, hostile_only, hostile_fallback)` *(v1.1.0+; v1.2.0 adds `hostile_only` + `is_structured`)* | **Recommended first tool.** Auto-escalating ladder A/B/C → D → E1 → E2 in a single call. Set `hostile_only=True` to skip A/B/C for known-hostile vendors. Returns `pattern_used`, `is_structured` (sidecar's success verdict), and `hostile_skipped`. |
| `fetch_with_ladder(url, method, use_curl_cffi, extract_structured)` | HTTP fetch through the TLS-impersonation ladder. With `extract_structured=True` (v1.1.0+) also runs Pattern B + C. |
| `extract_product(html, base_url)` | Pattern B — schema.org Product+Offer parser. |
| `extract_microdata_price(html)` | Pattern C — `<meta itemprop="price">` parser. |
| `map_site(url, include_sitemap, fetch_seed, same_domain, max_urls, timeout_s)` | Discover a site's URLs from sitemaps + seed-page links. No browser, no LLM. Run before `crawl_site` to size the job; truncation is always reported. |
| `crawl_site(url, schema_json, depth, max_pages, concurrency, same_domain, respect_robots, interactive, timeout_s)` | Breadth-first crawl running the full `auto_scrape` cascade per page, so recipe replay / render tier / proxy rotation all apply. Honours robots.txt incl. Crawl-delay. Page HTML omitted by default. |
| `canary(url, profiles)` | Walk the impersonation ladder and report which profile won. |
| `agent_extract(url, schema_json, instruction, model, browser, headful, timeout_s)` | **Pattern E1** — render with a stealth browser, 1 LLM call to extract structured JSON. Requires `[llm-agent]` extra. |
| `agent_browse(url, instruction, schema_json, model, browser, max_steps, headful, timeout_s)` | **Pattern E2** — multi-step browser-use agent loop for interactive tasks. Requires `[llm-agent]` extra. |

#### How it runs

The server speaks three transports — pick the one your client supports:

| Transport | Used by | How |
|-----------|---------|-----|
| **stdio** *(default)* | Claude Desktop, Claude Code (local) | Client spawns `scrapper-tool-mcp` as a subprocess; JSON-RPC over stdin/stdout. |
| **streamable-http** | Cursor, Claude Code (remote), mcp-use, any 2026 MCP-aware app | Long-running service; client connects via `url:` config. |
| **sse** | Older clients still on Server-Sent Events | Same as streamable-http but at `/sse`. |

```bash
pip install scrapper-tool[agent]            # MCP only
pip install scrapper-tool[agent,llm-agent]  # MCP + Pattern E

scrapper-tool-mcp                           # stdio (default)
scrapper-tool-mcp --transport streamable-http --host 0.0.0.0 --port 8765
scrapper-tool-mcp --help                    # full flag reference
```

Or via Docker (recommended — bundles all five patterns):

```bash
# HTTP service on host port 8765 — ready for Cursor / Claude Code / mcp-use:
SCRAPPER_TOOL_MCP_PORT=8765 \
SCRAPPER_TOOL_AGENT_LLM=openai_compat \
SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:1234 \
SCRAPPER_TOOL_AGENT_MODEL=qwen3-vl-8b-instruct \
docker compose --profile http up -d scrapper-tool-mcp-http
```

#### Wire into Claude Code / Cursor / Claude Desktop

#### Recommended — point at the Docker HTTP service

Once `docker compose --profile http up -d scrapper-tool-mcp-http` is running,
any URL-aware MCP client connects with one line:

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

This is the production shape: one warm container, many concurrent agents,
clean URL config, no per-call cold-start. Restart-as-a-service via
`docker compose --profile http restart scrapper-tool-mcp-http`.

#### Local-binary stdio (Claude Desktop pattern)

If your client only supports the spawn-a-binary pattern:

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

Or spawn the Docker container per call (Pattern E works on Windows hosts this
way because the agent runs Linux-side). This relies on the `scrapper-tool`
compose service declaring `entrypoint: ["scrapper-tool-mcp"]` — the image's
own ENTRYPOINT is the REST sidecar, so without that key the spawn attaches to
the wrong process and never speaks MCP:

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

For framework-specific wiring (AutoGen, LangChain, mcp-use, OpenClaw, Hermes
Agent), see **[`docs/agent-integration.md`](agent-integration.md)**.


---

## Running the bundled image as a stdio MCP server

The bundled Docker image's default entrypoint is `scrapper-tool-serve` (the REST sidecar on port 5792) since v1.1.2. To run it as a stdio MCP server instead, override the entrypoint.

## Docker run

```bash
docker run --rm -i \
    --entrypoint scrapper-tool-mcp \
    ghcr.io/valerok/scrapper-tool:latest
```

`-i` keeps stdin open — MCP-stdio clients pipe JSON-RPC over it.

## Docker compose

```yaml
services:
  scrapper-mcp:
    image: ghcr.io/valerok/scrapper-tool:latest
    entrypoint: ["scrapper-tool-mcp"]
    stdin_open: true
    tty: false
    # No port mapping — stdio MCP doesn't listen on a TCP port.
```

## Claude Desktop / mcp.json

```json
{
  "mcpServers": {
    "scrapper-tool": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--entrypoint", "scrapper-tool-mcp",
        "ghcr.io/valerok/scrapper-tool:latest"
      ]
    }
  }
}
```

## Why the entrypoint changed in v1.1.2

The README + [`http-sidecar.md`](http-sidecar.md) treat the REST sidecar as the primary surface for non-MCP callers. Pre-1.1.2 the image's default entrypoint was `scrapper-tool-mcp`, which forced every REST caller to override it (and the override was easy to miss — the failure mode was `unknown argument: 'scrapper-tool-serve'`). Flipping the default to `scrapper-tool-serve` matches the docs; MCP-mode users now carry the one-liner override above.

The `scrapper-tool-mcp` console script is unchanged. Both `scrapper-tool-serve` and `scrapper-tool-mcp` are installed in the image; only the *default* moved.

## All three console scripts

| Script | Mode | Default in v1.1.2 image? |
|---|---|---|
| `scrapper-tool-serve` | REST sidecar (FastAPI on :5792) | **yes** |
| `scrapper-tool-mcp` | Stdio MCP server | no — override `--entrypoint` |
| `scrapper-tool` | CLI (one-shot canary, etc.) | no — override `--entrypoint` |
