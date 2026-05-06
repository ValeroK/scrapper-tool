# HTTP REST sidecar

> **Available since v1.1.0.** Call scrapper-tool from any service over plain JSON/HTTP — no MCP client needed.

The REST sidecar exposes Patterns A through E as HTTP endpoints. Designed to run as a Docker sidecar on port **5792** alongside your application container.

---

## Quick start (3 commands)

```bash
# 1. Start the sidecar (uses docker-compose from this repo)
docker compose --profile rest up -d scrapper-tool-rest

# 2. Verify it's alive
curl http://localhost:5792/health
# → {"status": "ok"}

# 3. Scrape a product
curl -s -X POST http://localhost:5792/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/product/123"}'
```

That's it. The `/scrape` endpoint runs the full A/B/C → D → E1 → E2 ladder server-side and gives you back structured product data. Pattern D (Scrapling) is invoked between A/B/C and E1 when the `[hostile]` extra is installed (the bundled Docker image ships it via `[full]`); when it isn't, the cascade falls through to E1 and the response carries `hostile_skipped: true` so you know an LLM call was paid where Scrapling could have served the page directly.

---

## When to use which interface

| If you... | Use |
|---|---|
| Build a Python service and can `pip install` | **Python SDK** (`pip install scrapper-tool[llm-agent]`) |
| Build a non-Python service (Node, Go, PHP, Ruby, ...) | **HTTP sidecar** — this doc |
| Use Claude Code / Claude Desktop / Cursor | **MCP server** ([agent-integration.md](agent-integration.md)) |
| Need an LLM agent to pick scraping tools dynamically | **MCP server** |
| Run scraping from a worker that already speaks HTTP | **HTTP sidecar** |

---

## Endpoints at a glance

| Endpoint | Method | Auth | What it does |
|---|---|---|---|
| `/health` | GET | no | Liveness probe — always 200 |
| `/ready` | GET | no | Readiness with detailed component checks |
| `/version` | GET | no | Version + which extras are installed |
| `/scrape` | POST | optional | **Primary** — auto-escalating ladder A/B/C → D → E1 → E2 |
| `/fetch` | POST | optional | Pattern A/B/C — TLS-impersonation fetch + Pattern B/C extraction |
| `/extract` | POST | optional | Pattern E1 — Crawl4AI + LLM (1 LLM call) |
| `/browse` | POST | optional | Pattern E2 — browser-use multi-step agent |
| `/docs` | GET | no | Swagger UI (interactive playground) |
| `/redoc` | GET | no | ReDoc UI (read-friendly reference) |
| `/openapi.json` | GET | no | Raw OpenAPI 3.1 spec |

Auth: when `SCRAPPER_TOOL_HTTP_API_KEY` is set, the four POST endpoints require `X-API-Key: <value>`. The operational endpoints (`/health`, `/ready`, `/version`) and docs (`/docs`, `/redoc`, `/openapi.json`) are always unauthenticated so orchestrators can probe and clients can read the spec without credentials.

---

## `/scrape` — the main endpoint

The one you'll call 95% of the time. Give it a URL and (optionally) a schema, get back structured data plus a `pattern_used` field telling you which pattern produced it.

### Request

```json
{
  "url": "https://example.com/product/123",
  "schema_json": {"name": "str", "price": "float", "in_stock": "bool"},
  "instruction": "If on sale, set in_stock based on the sale-price visibility",
  "mode": "auto",
  "browser": "patchright",
  "model": "qwen3-vl:8b",
  "timeout_s": 60.0,
  "max_steps": 30,
  "headful": false,
  "force_llm_extract": false
}
```

All fields except `url` are optional. With no `schema_json`, you get an auto-detected `ProductOffer` from JSON-LD/microdata when A/B/C succeeds.

### When does `mode=auto` escalate? (1.1.2 + 1.1.3 behaviour)

A/B/C is treated as **success** (no escalation) when:

- The page returned 2xx **and**
- One of: a JSON-LD blob was found, microdata price was extracted, an auto-detected `product` was extracted, OR `mode="fetch"` was forced.

If you supply `schema_json` and A/B/C returned a readable page (2xx + any structured signal), the response carries `pattern_used="a_b_c"` and the raw `text` / `json_ld` / `microdata_price` so your code can post-process locally — no LLM call needed.

**Pre-1.1.2 behaviour:** any `schema_json` request always escalated to E1, even on trivial Pattern-B HTML. That wasted ~0.5–60s per request (LLM cost + latency). Set `force_llm_extract: true` if you genuinely need the LLM to apply your custom schema even when A/B/C had structured output. Most callers see lower latency + lower LLM cost as a free upgrade.

**Pattern D (1.1.3+):** when A/B/C is blocked or the page lacks any structured signal, the cascade now tries Pattern D (Scrapling's `StealthyFetcher` with auto-Turnstile-solve) before paying for an LLM call. D succeeds on most Cloudflare-Turnstile-protected vendors and is roughly 10–30× cheaper than E1 because it doesn't invoke the LLM. D is invoked when the `[hostile]` extra is installed; when it isn't, the response carries `hostile_skipped: true` and the cascade goes straight to E1 — operators see the warning in `/ready` so they know to install `pip install scrapper-tool[hostile]` to recover the cost win.

### Why install `[hostile]`?

Without it, every hostile-vendor request that A/B/C can't read pays for E1 (~$0.001–0.01 in tokens + 5–15 s of browser warm-up). With it, Pattern D handles Turnstile-protected pages in ~2–4 s with no LLM tokens. The bundled Docker image (`ghcr.io/valerok/scrapper-tool`) ships `[hostile]` via `[full]`, so you only need to install it explicitly when running the lib outside the published image (lean Python install, custom container, etc.).

### How to read the response (1.2.0+)

Every `/scrape` response carries `is_structured: bool` — the sidecar's classifier verdict:

- **`is_structured: true`** — the page yielded a real payload. `product` / `json_ld` / `microdata_price` / `data` carry usable structured data.
- **`is_structured: false`** — soft failure. May still have `data` (E1's `_raw` free-form text) or be fully blocked. Treat as a non-result regardless of `pattern_used`.

Downstream consumers should use `payload.get("is_structured", False)` instead of deriving the verdict from response shape — the sidecar already classifies via `_classify_extraction_success` (A/B/C, D) and `_is_e_tier_structured` (E1, E2).

### Response — fast path (Pattern A/B/C succeeded)

```json
{
  "url": "https://example.com/product/123",
  "pattern_used": "a_b_c",
  "pattern_attempts": ["a_b_c"],
  "product": {
    "name": "Widget Pro X",
    "brand": "WidgetCo",
    "price": "29.99",
    "currency": "USD",
    "availability": "https://schema.org/InStock"
  },
  "data": null,
  "raw_text": "<!DOCTYPE html>...",
  "json_ld": [{"@type": "Product", "name": "Widget Pro X"}],
  "microdata_price": {"price": "29.99", "currency": "USD"},
  "blocked": false,
  "hostile_skipped": false,
  "is_structured": true,
  "duration_s": 0.83
}
```

`product.price` is a **string** (not a float) — Python's `Decimal` serialises as string in pydantic v2 to avoid floating-point precision loss. Use `parseFloat(product.price)` (JS) or `float(product["price"])` (Python) if you need a number.

### Response — Pattern D won (1.1.3+)

```json
{
  "url": "https://hostile.com/product/789",
  "pattern_used": "d",
  "pattern_attempts": ["a_b_c", "d"],
  "product": {"name": "Turnstile Widget", "price": "39.99", "currency": "USD"},
  "data": null,
  "raw_text": "<!DOCTYPE html>...",
  "json_ld": [...],
  "microdata_price": {"price": "39.99", "currency": "USD"},
  "blocked": false,
  "hostile_skipped": false,
  "is_structured": true,
  "duration_s": 2.91
}
```

A/B/C was blocked but Scrapling's `StealthyFetcher` (with `solve_cloudflare=true`) got past the WAF, and Pattern B/C extracted structured data from the resulting HTML. No LLM tokens consumed.

### Response — escalated to E1

```json
{
  "url": "https://protected.com/product/456",
  "pattern_used": "e1",
  "pattern_attempts": ["a_b_c", "d", "e1"],
  "product": null,
  "data": {"name": "Protected Widget", "price": 49.99, "in_stock": true},
  "rendered_markdown": "# Protected Widget\n\n**Price:** $49.99...",
  "tokens_used": 1247,
  "hostile_skipped": false,
  "is_structured": true,
  "duration_s": 8.34
}
```

When the auto-escalation falls back to E1 (Pattern A/B/C blocked AND Pattern D either failed or wasn't installed), the LLM applies your `schema_json` to the rendered page. `data` holds the structured result. Watch `hostile_skipped: true` — when present alongside `pattern_used="e1"|"e2"`, you paid for an LLM call that Pattern D could have served for free; install `scrapper-tool[hostile]` to recover the cost win on subsequent calls.

Watch `is_structured: false` even when `blocked: false` — that's the LLM-narrated-failure case. Crawl4AI returns `data: {"_raw": "..."}` when the LLM produced free-form text rather than valid JSON against your schema. Treat as a non-result.

### Forcing a specific pattern

Set `mode` to skip the auto-escalation:
- `mode="fetch"` — only run A/B/C (raw fetch + structured extraction). Never invokes D.
- `mode="extract"` — go straight to Pattern E1.
- `mode="browse"` — go straight to Pattern E2.
- `mode="hostile"` *(v1.2.0+)* — invoke Pattern D directly, skipping A/B/C. On D failure, falls back to E1/E2 unless `hostile_fallback: false`. Use for vendors recon-classified as hostile (Cloudflare Turnstile, Akamai EVA, DataDome) where A/B/C is known to fail. Saves ~2-3s per call by skipping the 4 doomed `curl_cffi` profile attempts.

When `hostile_fallback: false`, a missing `[hostile]` extra returns 503 (with install hint); a D-fetch failure returns 422 (`error: "blocked"`). Use `false` on adapters that have already paid the cost of recon and want to fail fast rather than silently pay for an LLM call.

**For SPA-rendered hostile vendors** *(v1.2.0+)*, set `pattern_d_network_idle: true`. Adds ~5-15s of fetch latency but lets the page hydrate before D captures HTML — required for sites where results lazy-load via JS after CF clearance (Tasca search, RevolutionParts dealers). Pairs with `mode: "hostile"` for known-hostile SPA targets:

```bash
curl -s -X POST http://localhost:5792/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://tascaparts.com/search?q=90915-YZZD4",
    "mode": "hostile",
    "pattern_d_network_idle": true
  }'
```

### Cross-request CF clearance reuse (v1.3.0+)

By default v1.3.0 allocates a per-cascade ephemeral browser profile dir, threads it to Pattern D + E1 + E2, then deletes it on every exit path. This eliminates the redundant CF challenges that pre-1.3.0 made E-tier escalations fresh-fight already-bypassed sites: D solves Cloudflare once, captures the HTML, and any E1/E2 fallback launches against the same on-disk cookie jar (including `cf_clearance`). Net effect on Tasca: ~70s + 2 LLM calls + no result becomes ~25s + 1 LLM call + result.

For poll-style adapters (the dominant Tasca pattern: hit the same domain every 5 minutes), pass `persist_browser_profile_dir` to opt into cross-request reuse. The caller owns the lifecycle:

```bash
curl -s -X POST http://localhost:5792/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://tascaparts.com/search?q=90915-YZZD4",
    "mode": "hostile",
    "pattern_d_network_idle": true,
    "persist_browser_profile_dir": "/var/lib/scrapper/profiles/tasca/"
  }'
```

**Caller responsibilities** with `persist_browser_profile_dir`:

1. **Per-vendor isolation** — use a different dir per host (e.g. `/profiles/tasca/`, `/profiles/amayama/`). CF clearance cookies are domain-bound but cross-vendor profile reuse leaks fingerprint state, which CF flags eventually.
2. **Periodic rotation** — `cf_clearance` cookies have a ~30 min TTL. Long-lived profiles get flagged as suspicious if reused across many requests. Rotate the dir every 30 min via cron or your own scheduler.
3. **Cleanup** — the sidecar will NEVER delete a caller-provided dir. Without rotation, the dir accumulates session state indefinitely.

When `persist_browser_profile_dir` is unset, the cascade ephemeral default applies and you get the within-cascade benefit without any operator burden.

If your runtime's Crawl4AI or browser-use version silently ignores `user_data_dir` (older releases), `/ready` emits a `user_data_dir_unsupported` warning so you can upgrade. Check `GET /ready | jq .checks.user_data_dir_supported` to confirm the runtime can carry CF state forward.

---

## `/fetch`, `/extract`, `/browse` — power-user control

For callers that want fine-grained control over which pattern runs.

### POST /fetch — Pattern A/B/C

```json
// request
{"url": "https://target.com/p/123", "extract_structured": true}

// response
{
  "status_code": 200,
  "profile": "chrome133a",        // winning impersonation profile
  "text": "<!DOCTYPE html>...",
  "headers": {"content-type": "text/html"},
  "product": {"name": "...", "price": "29.99", ...},
  "json_ld": [...],
  "microdata_price": {"price": "29.99", "currency": "USD"}
}
```

### POST /extract — Pattern E1 (1 LLM call)

```json
// request
{
  "url": "https://target.com/p/123",
  "schema_json": {"title": "str", "price": "float"},
  "model": "qwen3-vl:8b"
}

// response: an AgentResult
{
  "mode": "extract",
  "data": {"title": "Widget Pro X", "price": 29.99},
  "final_url": "https://target.com/p/123",
  "rendered_markdown": "# Widget Pro X...",
  "tokens_used": 1247,
  "duration_s": 8.12
}
```

### POST /browse — Pattern E2 (interactive agent)

```json
// request
{
  "url": "https://target.com/login",
  "instruction": "Log in with demo/demo, navigate to /deals, return the first 5 product names and prices",
  "max_steps": 30
}

// response: an AgentResult with multi-step trace
{
  "mode": "browse",
  "data": {"products": [{"name": "...", "price": ...}]},
  "actions": [
    {"step": 1, "action": "goto", "target": "https://target.com/login"},
    {"step": 2, "action": "type", "target": "input[name=user]"},
    ...
  ],
  "screenshots": ["iVBORw0KGgo..."],
  "tokens_used": 8734,
  "steps_used": 12
}
```

---

## Error codes

All errors share the same shape: `{"error": "<code>", "detail": "<human message>"}`.

| HTTP | `error` | When |
|---|---|---|
| 422 | `blocked` (with `"blocked": true`) | All patterns blocked / anti-bot |
| 502 | `llm_unreachable` | Ollama / LLM server can't be reached |
| 502 | `vendor_http_error` | Target site returned 5xx / transport errors after retries |
| 503 | `configuration_error` | Local environment misconfigured (browser binary missing, extra not installed, model not pulled) |
| 504 | `agent_timeout` | Agent loop exceeded `timeout_s` |
| 500 | `agent_error` / `scraping_error` | Unexpected internal failure |

Examples:
```json
// Anti-bot blocked everything
{"error": "blocked", "detail": "All patterns blocked: a_b_c, e1, e2", "blocked": true}

// Local install missing
{"error": "configuration_error", "detail": "patchright binary not found. Run: uv run patchright install chromium"}

// LLM down
{"error": "llm_unreachable", "detail": "Cannot connect to Ollama at http://localhost:11434"}
```

---

## `/ready` — readiness probe

Useful for orchestrators (Kubernetes, ECS) and for the affiliate service to verify the sidecar is fully operational before sending real traffic.

```json
{
  "status": "ready",
  "version": "1.1.2",
  "checks": {
    "agent_installed": true,
    "agent_runnable": true,
    "hostile_installed": true,
    "browser": "patchright",
    "browser_binary": "ok",
    "llm_backend": "ollama",
    "llm_url": "http://localhost:11434",
    "llm_reachable": true,
    "llm_model": "qwen3-vl:8b",
    "llm_model_available": true
  }
}
```

`status` values:
- `ready` — everything works, safe to send traffic
- `degraded` — sidecar can serve A/B/C cheaply (`/fetch`, `/scrape mode=fetch`) but Pattern E is not available (browser missing, LLM unreachable, model not loaded, etc.)
- `not_ready` — `[llm-agent]` extra not installed (fetch-only mode). E1/E2 endpoints will return 503

`checks.agent_installed` vs `checks.agent_runnable` (since v1.1.2):
- `agent_installed` — the `[llm-agent]` Python extra is importable. Necessary but not sufficient.
- `agent_runnable` — `agent_installed` AND the on-disk binary for the configured `SCRAPPER_TOOL_AGENT_BROWSER` is present (Camoufox / Patchright Chromium / Playwright Firefox). **Operators should gate Pattern E calls on this.** Pre-1.1.2 there was only `agent_installed`, which silently returned true even when the binary was missing.

The endpoint always returns HTTP 200 — the body distinguishes "sidecar crashed" (no response) from "sidecar up but degraded" (degraded).

The endpoint always returns HTTP 200 — the body distinguishes "sidecar crashed" (no response) from "sidecar up but LLM unavailable" (degraded).

---

## Configuration

Just the HTTP-server-specific environment variables. For the full agent / browser / captcha env-var matrix see [`SETTINGS.md`](SETTINGS.md).

| Env var | Default | Notes |
|---|---|---|
| `SCRAPPER_TOOL_HTTP_HOST` | `0.0.0.0` | Bind address. `127.0.0.1` to restrict to localhost |
| `SCRAPPER_TOOL_HTTP_PORT` | `5792` | TCP port |
| `SCRAPPER_TOOL_HTTP_API_KEY` | (unset) | When set, `X-API-Key: <value>` required on POST endpoints |
| `SCRAPPER_TOOL_HTTP_CORS_ORIGINS` | `*` | Comma-separated CORS allowed origins |
| `SCRAPPER_TOOL_HTTP_LOG_LEVEL` | `info` | `debug` / `info` / `warning` / `error` / `critical` |
| `SCRAPPER_TOOL_HTTP_DOCS` | `1` | Set `0` to disable `/docs` and `/redoc` (production) |

---

## Affiliate service wiring

### docker-compose.yml (sidecar pattern)

```yaml
services:
  affiliate:
    image: my-org/affiliate:latest
    environment:
      SCRAPPER_TOOL_BASE_URL: "http://scrapper-tool-rest:5792"
    depends_on:
      scrapper-tool-rest:
        condition: service_healthy

  scrapper-tool-rest:
    image: ghcr.io/valerok/scrapper-tool:1.1.0
    entrypoint: ["scrapper-tool-serve"]
    ports:
      - "5792:5792"
    environment:
      SCRAPPER_TOOL_AGENT_OLLAMA_URL: http://host.docker.internal:11434
      SCRAPPER_TOOL_AGENT_MODEL: qwen3-vl:8b
      SCRAPPER_TOOL_AGENT_BROWSER: patchright
      SCRAPPER_TOOL_CAPTCHA_SOLVER: auto
    extra_hosts:
      - "host.docker.internal:host-gateway"
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:5792/health || exit 1"]
      interval: 30s
      timeout: 5s
```

### Python client (in the affiliate service)

```python
import httpx

class ScrapperClient:
    def __init__(self, base_url: str = "http://scrapper-tool-rest:5792",
                 api_key: str | None = None) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=150.0
        )

    async def scrape(self, url: str, *, schema: dict | None = None) -> dict:
        resp = await self._http.post("/scrape", json={"url": url, "schema_json": schema})
        resp.raise_for_status()
        return resp.json()

    async def health(self) -> bool:
        try:
            r = await self._http.get("/health", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False
```

### TypeScript client (codegen)

The committed `docs/openapi/openapi.yaml` lets the affiliate service generate a typed TypeScript client without writing any HTTP code by hand:

```bash
npx openapi-typescript-codegen \
  --input docs/openapi/openapi.yaml \
  --output ./src/scrapper-client
```

Then in your code:
```ts
import { ScrapeService } from "./scrapper-client";

const result = await ScrapeService.scrape({
  url: "https://target.com/product/123",
  schema_json: { name: "str", price: "float" },
});
console.log(result.pattern_used, result.data ?? result.product);
```

For Python: `uv run openapi-python-client generate --path docs/openapi/openapi.yaml`.

---

## LLM reference

> Section for LLM agents reading these docs. Contains the full schema; the human-readable sections above are sufficient for most callers.

### Spec files

- **Live**: `http://<host>:5792/openapi.json` (served by the running container)
- **Static**: [`docs/openapi/openapi.yaml`](openapi/openapi.yaml) and [`docs/openapi/openapi.json`](openapi/openapi.json) (committed; regenerate with `uv run python scripts/dump_openapi.py`)

### Cross-references

- Settings env-var matrix: [`SETTINGS.md`](SETTINGS.md)
- MCP server (alternative integration): [`agent-integration.md`](agent-integration.md)
- Pattern E (LLM agent layer) deep dive: [`patterns/e-llm-agent.md`](patterns/e-llm-agent.md)
- Source: [`src/scrapper_tool/http_server.py`](../src/scrapper_tool/http_server.py)

### Endpoint operationIds (for OpenAPI codegen)

| Endpoint | operationId | Tag |
|---|---|---|
| GET /health | `health` | operational |
| GET /ready | `ready` | operational |
| GET /version | `version` | operational |
| POST /scrape | `scrape` | scraping |
| POST /fetch | `fetch` | scraping |
| POST /extract | `extract` | agent |
| POST /browse | `browse` | agent |

### Request schema names

`FetchRequest`, `ScrapeRequest`, `ExtractRequest`, `BrowseRequest` — defined at module scope in `src/scrapper_tool/http_server.py` so OpenAPI codegen picks up stable names.

### Response shape pointers

- `/scrape` returns the dict shape documented in the [/scrape section](#scrape--the-main-endpoint) above. `pattern_used` is one of `"a_b_c" | "d" | "e1" | "e2"`. `hostile_skipped: true` indicates Pattern D was unreachable because the `[hostile]` extra wasn't installed.
- `/extract` and `/browse` return `AgentResult.model_dump(mode="json")` — see `src/scrapper_tool/agent/types.py` for the full pydantic schema. Bytes fields (`screenshots`) are base64-encoded strings.
- `/fetch` returns the dict in [the /fetch section](#post-fetch--pattern-abc).

### Rate limiting and retries

The sidecar has no built-in rate limiting. Callers should:
- Implement client-side concurrency limits (the affiliate service typically caps at 3-5 parallel scrapes per sidecar)
- Use exponential backoff on 502/503/504 (transient LLM/browser issues)
- Treat 422 (`blocked: true`) as terminal — don't retry; the target site has flagged us

### Why not auth-by-default?

The sidecar is designed to run on an internal Docker network that is not exposed to the public internet. When `SCRAPPER_TOOL_HTTP_API_KEY` is unset, any service on the network can call it — fine for trusted internal traffic. Set the env var for defense-in-depth or when exposing the sidecar via an ingress.
