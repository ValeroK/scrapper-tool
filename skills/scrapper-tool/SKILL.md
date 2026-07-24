---
name: scrapper-tool
description: >-
  Use when the task is to get data off a website — scrape a page, extract a
  product/price, pull structured fields, discover a site's URLs, or crawl a
  whole site — especially when the site is protected by anti-bot systems
  (Cloudflare, DataDome, Akamai, PerimeterX) or renders its content with
  JavaScript. scrapper-tool is a Python toolkit with an auto-escalating cascade
  that tries the cheapest method first and climbs to a stealth browser or a
  local LLM only when the site forces it. Reach for this instead of a bare
  `requests`/`fetch` when a plain GET would be blocked or would return an empty
  JS shell.
---

# scrapper-tool

scrapper-tool turns "get the data from this URL" into one call. Behind that call
is a **cascade** of extraction tiers, cheapest first; it stops at the first tier
that reaches real content, learns a cheap recipe from any expensive win, and
remembers per-domain which tier worked so the next request is faster.

You do **not** pick a tier. You call one entrypoint and read `pattern_used` from
the result to see which tier won.

## The cascade (the mental model)

```
replay   cached recipe → deterministic parse        cheapest, often free
a_b_c    curl_cffi TLS-impersonation HTTP + parse
d        Scrapling (hostile fetcher, solves Cloudflare)
render   stealth browser (Camoufox) + parse         NO LLM
e1       Crawl4AI + local LLM (one call)
e2       browser-use agent (multi-step)             priciest, opt-in only
```

- It **auto-escalates**: a page that a plain fetch can read is served by `a_b_c`;
  a JS-rendered or 403-walled page climbs to `render`; only genuinely unstructured
  or interactive pages reach the LLM tiers.
- **`e2` (the interactive agent) never runs unless you ask for it** with
  `interactive=true`. It's for login / pagination / dynamic forms — a merely
  *walled* page will wall the agent too, for far more cost.
- Success is judged on **content, not status code**: a 403 that carries a real
  rendered DOM (common with Akamai) counts as a win.

## Three ways to call it — pick one

| You are… | Use | How |
|----------|-----|-----|
| An MCP client (Claude Desktop/Code, Cursor, AutoGen, LangChain) | the **MCP server** | tool `auto_scrape` |
| Writing Python | the **library** | `await scrape(url)` |
| A service in another language | the **REST sidecar** | `POST /scrape` |

All three run the *same* cascade — they can't give different answers.

---

## MCP tools

Start the server with the `scrapper-tool-mcp` command (needs `pip install
'scrapper-tool[agent]'`). Register it in your client's MCP config, e.g. `.mcp.json`:

```json
{ "mcpServers": { "scrapper-tool": { "command": "scrapper-tool-mcp", "args": [] } } }
```

**Primary tool — `auto_scrape`.** Use this for almost everything.

```
auto_scrape(
  url,                       # required
  schema_json = null,        # optional: shape to extract (see "Schemas" below)
  instruction = null,        # optional: natural-language extraction hint (LLM tiers)
  interactive = false,       # true → allow the E2 agent for login/pagination/forms
  browser = null,            # "camoufox" (default) | "patchright" | "obscura"
  timeout_s = 120,
  hostile_only = false,      # skip the HTTP ladder, start at Scrapling (known-hard sites)
)
```

It returns `pattern_used`, `product`, `data`, `body`, `challenge_detected`,
`blocked`, and more (see "Reading the result").

**Site-level tools:**

- `map_site(url, max_urls=200, same_domain=true, include_sitemap=true)` — discover
  a site's URLs (sitemaps + page links). Cheap, no browser. Run this before a
  crawl to see how big the job is.
- `crawl_site(url, schema_json=null, depth=2, max_pages=25, concurrency=4,
  interactive=false)` — crawl breadth-first, running the full cascade per page.
  Each page benefits from the recipe learned on the first, so a crawl gets cheaper
  as it goes.

**Lower-level tools** (use only when you specifically need one, not for general
scraping): `fetch_with_ladder`, `extract_product(html)`,
`extract_microdata_price(html)`, `agent_extract`, `agent_browse`, `canary`.

---

## Python library

```python
from scrapper_tool import scrape

result = await scrape("https://store.example.com/product/123")
print(result["pattern_used"], result["product"])
```

`scrape(url, schema=None, *, interactive=False, mode="auto", browser=None,
model=None, timeout_s=None, instruction=None)` — same params as `auto_scrape`.

```python
# Structured extraction with a CSS schema:
result = await scrape(url, schema={
    "baseSelector": "div.product-card",
    "fields": [
        {"name": "title", "selector": "h3", "type": "text"},
        {"name": "price", "selector": ".price", "type": "text"},
    ],
})
rows = result["data"]

# Crawl a whole site (streams a result per page):
from scrapper_tool import crawl_site
async for page in crawl_site("https://shop.example.com/", depth=2, max_pages=50):
    if page.ok:
        print(page.url, page.payload["pattern_used"])

# Just discover URLs, no scraping:
from scrapper_tool.crawl import map_site, make_ladder_fetch
result = await map_site("https://shop.example.com/", fetch=make_ladder_fetch())
```

Low-level building blocks are still exported for when the cascade is overkill:
`vendor_client`, `request_with_retry`, `request_with_ladder`, and the pattern
extractors in `scrapper_tool.patterns.{b,c}`.

---

## REST sidecar

Start with `scrapper-tool-serve` (needs `pip install 'scrapper-tool[http]'`),
default port 5792.

```
POST /scrape   {"url": "...", "schema_json": {...}, "interactive": false}
POST /map      {"url": "...", "max_urls": 200}
POST /crawl    {"url": "...", "depth": 2, "max_pages": 50}
GET  /health   /ready   /metrics    (Prometheus)
```

`POST /scrape` takes the same fields as `auto_scrape` and returns the same
payload. Also: `/fetch` (tier 1 only), `/extract` (force E1), `/browse` (force E2).

---

## Schemas — how to ask for structured data

Pass `schema_json` (MCP/REST) or `schema=` (library) in one of three forms:

1. **CSS schema** (deterministic, no LLM — prefer this for lists/tables):
   ```json
   {"baseSelector": "li.result",
    "fields": [{"name": "title", "selector": "h2", "type": "text"},
               {"name": "url", "selector": "a", "type": "attribute", "attribute": "href"}]}
   ```
   Field `type` is `text` | `attribute` (+ `attribute` key) | `html` | `int` | `float`.
2. **JSON Schema dict** — hands the page to the LLM tier to fill.
3. **Natural-language string** — e.g. `"the product name, price, and SKU"`.

With **no schema**, the cascade auto-extracts a product from JSON-LD/microdata
when present, and otherwise returns the page body/markdown.

---

## Reading the result

Every scrape returns a dict. The keys that matter:

| Key | Meaning |
|-----|---------|
| `pattern_used` | which tier won: `replay` / `a_b_c` / `d` / `render` / `e1` / `e2` |
| `product` | auto-detected product+price (JSON-LD/microdata), or null |
| `data` | rows from a CSS schema, or null |
| `raw_text` / `body` | the fetched/rendered HTML |
| `challenge_detected` | the bot-vendor that walled us (`cloudflare`, `datadome`, …) or null |
| `blocked` | true when every tier failed |
| `is_structured` | whether a real structured payload was produced |
| `escalation_log` | per-tier trace: what ran, what it cost, why it escalated |

**How to act on it:** if `blocked` is true, the site defeated every available
tier — retry with `interactive=true` only if the page genuinely needs
interaction, otherwise report it as unreachable (it may need a residential proxy,
which the operator supplies via `SCRAPPER_TOOL_PROXIES`, not something you can
fix in the call). If `pattern_used` is `e1`/`e2`, a local LLM was used — that's
normal for hard/unstructured pages but slower; a CSS `schema` avoids it when the
data is in the DOM.

---

## Decision guide

- **"Get the data from this URL"** → `auto_scrape(url)` / `scrape(url)`. Add a
  `schema` if you want specific fields.
- **"Get every product on this site"** → `map_site` first to gauge size, then
  `crawl_site` with a `schema`.
- **"It needs me to log in / click through pages / fill a form"** → add
  `interactive=true` (this is the only case that unlocks the E2 agent).
- **"It's a known-hard site (Cloudflare/Akamai)"** → just call `auto_scrape`; the
  cascade escalates on its own. Optionally `hostile_only=true` to skip the
  ladder and save ~2 s.
- **"I already have the HTML"** → `extract_product(html)` /
  `extract_microdata_price(html)`, no fetch.

## Gotchas

- The LLM tiers (`e1`/`e2`) need the `[llm-agent]` extra and a running local LLM
  (Ollama by default, `SCRAPPER_TOOL_AGENT_LLM` / `_MODEL` / `_OLLAMA_URL` to
  configure). Without them the cascade still runs A/B/C/D/render and reports
  `hostile_skipped` / stops rather than crashing.
- `render` (Camoufox) needs `[llm-agent]` installed and `camoufox fetch` run once.
- The cascade caches learned recipes and per-domain tier memory under a temp dir;
  set `SCRAPPER_TOOL_RECIPE_DIR` to relocate, or `SCRAPPER_TOOL_RECIPE_CACHE=0` /
  `SCRAPPER_TOOL_DOMAIN_POLICY=0` to disable.
- The toolkit cannot manufacture IP reputation. If a site blocks every tier from
  your IP, that's an IP-trust limit — a residential/mobile proxy is the fix, not
  a different tier.

## Reference

- Full settings: `docs/SETTINGS.md`
- Per-tool MCP wiring for each framework: `docs/agent-integration.md`
- The cascade tiers in depth: `docs/patterns/`
