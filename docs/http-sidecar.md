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

That's it. The `/scrape` endpoint runs the full A/B/C → E1 → E2 ladder server-side and gives you back structured product data.

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
| `/scrape` | POST | optional | **Primary** — auto-escalating ladder A/B/C → E1 → E2 |
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
  "headful": false
}
```

All fields except `url` are optional. With no `schema_json`, you get an auto-detected `ProductOffer` from JSON-LD/microdata when A/B/C succeeds.

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
  "duration_s": 0.83
}
```

`product.price` is a **string** (not a float) — Python's `Decimal` serialises as string in pydantic v2 to avoid floating-point precision loss. Use `parseFloat(product.price)` (JS) or `float(product["price"])` (Python) if you need a number.

### Response — escalated to E1

```json
{
  "url": "https://protected.com/product/456",
  "pattern_used": "e1",
  "pattern_attempts": ["a_b_c", "e1"],
  "product": null,
  "data": {"name": "Protected Widget", "price": 49.99, "in_stock": true},
  "rendered_markdown": "# Protected Widget\n\n**Price:** $49.99...",
  "tokens_used": 1247,
  "duration_s": 8.34
}
```

When the auto-escalation falls back to E1 (Pattern A/B/C was blocked), the LLM applies your `schema_json` to the rendered page. `data` holds the structured result.

### Forcing a specific pattern

Set `mode` to skip the auto-escalation:
- `mode="fetch"` — only run A/B/C (raw fetch + structured extraction)
- `mode="extract"` — go straight to Pattern E1
- `mode="browse"` — go straight to Pattern E2

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
  "version": "1.1.0",
  "checks": {
    "agent_installed": true,
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
- `degraded` — agent installed but something is off (model not pulled, browser missing). Calls may fail
- `not_ready` — `[llm-agent]` extra not installed (fetch-only mode). E1/E2 endpoints will return 503

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

- `/scrape` returns the dict shape documented in the [/scrape section](#scrape--the-main-endpoint) above. `pattern_used` is one of `"a_b_c" | "e1" | "e2"`.
- `/extract` and `/browse` return `AgentResult.model_dump(mode="json")` — see `src/scrapper_tool/agent/types.py` for the full pydantic schema. Bytes fields (`screenshots`) are base64-encoded strings.
- `/fetch` returns the dict in [the /fetch section](#post-fetch--pattern-abc).

### Rate limiting and retries

The sidecar has no built-in rate limiting. Callers should:
- Implement client-side concurrency limits (the affiliate service typically caps at 3-5 parallel scrapes per sidecar)
- Use exponential backoff on 502/503/504 (transient LLM/browser issues)
- Treat 422 (`blocked: true`) as terminal — don't retry; the target site has flagged us

### Why not auth-by-default?

The sidecar is designed to run on an internal Docker network that is not exposed to the public internet. When `SCRAPPER_TOOL_HTTP_API_KEY` is unset, any service on the network can call it — fine for trusted internal traffic. Set the env var for defense-in-depth or when exposing the sidecar via an ingress.
