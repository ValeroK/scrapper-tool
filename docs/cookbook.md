# Sidecar cookbook

Working examples for the canonical vendor shapes. Pick the recipe that matches what you're scraping; copy the request body verbatim; tune from there.

All examples assume the sidecar is reachable at `http://localhost:5792` and the published v1.4.0 image (or newer).

---

## Recipe 1 — Server-rendered LD+JSON product (Amayama, Subaru-JP)

The simplest hostile-vendor case. The page emits a `<script type="application/ld+json">{"@type": "Product", ...}</script>` block. Pattern D fetches the HTML, the built-in JSON-LD extractor reads the block, you get a normalised `product` dict.

```bash
curl -s -X POST http://localhost:5792/scrape \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"url":"https://www.amayama.com/en/find?q=90915-YZZD4"}'
```

Response (abridged):

```json
{
  "pattern_used": "d",
  "is_structured": true,
  "product": {
    "name": "Toyota 90915-YZZD4 (90915YZZD4)",
    "brand": "Toyota",
    "mpn": "90915-YZZD4",
    "currency": "USD",
    "availability": "https://schema.org/InStock"
  },
  "duration_s": 17.5,
  "escalation_log": [
    {"step": "a_b_c", "outcome": "failed", "reason": "blocked", "duration_s": 0.4},
    {"step": "d", "outcome": "won", "reason": "ok", "duration_s": 17.1}
  ]
}
```

No `schema_json` needed. Cost: ~17s cold, 0 LLM tokens.

---

## Recipe 2 — SPA hostile vendor with extractable result cards (Tasca, RevolutionParts dealers)

The page is rendered client-side. After CF clearance and JS hydration, results appear as repeated `<div class="search-result-item">` cards. No LD+JSON. Pattern D fetches the hydrated HTML; the CSS extractor pulls rows out via selectolax.

```bash
curl -s -X POST http://localhost:5792/scrape \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "url": "https://www.tascaparts.com/search?q=BR3Z6731A",
    "schema_json": {
      "baseSelector": "div.search-result-item",
      "fields": [
        {"name": "title", "selector": "h3.product-title", "type": "text"},
        {"name": "price", "selector": "span.price", "type": "text"},
        {"name": "url", "selector": "a.product-link",
         "type": "attribute", "attribute": "href"},
        {"name": "vendor_item_id", "selector": "a.product-link",
         "type": "attribute", "attribute": "data-sku"}
      ]
    }
  }'
```

Response (abridged):

```json
{
  "pattern_used": "d",
  "is_structured": true,
  "data": [
    {"title": "Front Brake Pad Set", "price": "89.99",
     "url": "/parts/br3z6731a", "vendor_item_id": "BR3Z6731A"},
    {"title": "...", "...": "..."}
  ],
  "duration_s": 22.3
}
```

The sidecar's smart defaults handle the SPA hydration retry — no need to set `pattern_d_network_idle=true` explicitly. The schema's CSS shape is what tells the sidecar to use the CSS extractor.

---

## Recipe 3 — D-then-custom-parser hybrid (Megazip)

Use this when the vendor's results aren't a clean CSS pattern but a complex DOM that your existing in-process parser already handles. Pattern D defeats CF, returns HTML; you parse the HTML yourself.

```bash
curl -s -X POST http://localhost:5792/scrape \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "url": "https://www.megazip.net/search?q=04152-YZZA6",
    "mode": "hostile"
  }'
```

Then in your adapter:

```python
result = await client.scrape(url, mode="hostile")
html = result.intermediate_raw_text or result.raw_text
items = my_in_process_parser(html)  # selectolax / beautifulsoup / etc.
```

**Why `intermediate_raw_text`**: even if Pattern D's classifier rejects (no LD+JSON / no CSS schema match), the raw HTML it fetched is always exposed via `intermediate_raw_text`. Your parser doesn't depend on the sidecar's success classifier.

---

## Recipe 4 — LLM extraction with strict schema (catch-all)

When the page has no extractable structure (everything in JavaScript state, no LD+JSON, no clean CSS pattern), let Pattern E1 (Crawl4AI + LLM) render and extract:

```bash
curl -s -X POST http://localhost:5792/scrape \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "url": "https://hard-to-scrape.com/p/12345",
    "schema_json": {
      "type": "object",
      "properties": {
        "name": {"type": "string"},
        "price": {"type": "number"},
        "in_stock": {"type": "boolean"}
      },
      "required": ["name"]
    }
  }'
```

The Pydantic-shaped `schema_json` tells the sidecar to route to E1. Cost: ~30-60s + LLM tokens. Use this when CSS extraction isn't feasible.

---

## Recipe 5 — Pure Pattern D, no escalation (cost-sensitive batch jobs)

When you're scraping at scale and want to fail fast on Pattern D rather than silently paying for an LLM call:

```bash
curl -s -X POST http://localhost:5792/scrape \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "url": "https://hostile.com/p/123",
    "mode": "hostile",
    "hostile_fallback": false
  }'
```

When Pattern D fails, returns 422 (or 503 if `[hostile]` isn't installed) instead of escalating to E1/E2. Caller decides whether to retry.

---

## Recipe 6 — Cross-request CF clearance reuse (poll-style workloads)

When you're hitting the same hostile domain every few minutes and want to amortise the CF challenge across requests:

```bash
# Per-vendor profile dir on a writable volume
curl -s -X POST http://localhost:5792/scrape \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "url": "https://hostile.com/poll-target",
    "persist_browser_profile_dir": "/var/lib/scrapper/profiles/hostile-com/"
  }'
```

**Operator responsibilities**:

1. Per-vendor isolation. Use a different dir per host. Cookies are domain-bound but cross-vendor profile reuse leaks fingerprint state, which Cloudflare flags eventually.
2. ~30 minute rotation. The `cf_clearance` cookie has roughly that TTL; long-lived profiles get flagged as suspicious. Rotate via cron or your scheduler.
3. The sidecar will NEVER delete a caller-provided dir. Cleanup is your job.

When `persist_browser_profile_dir` is unset, the cascade allocates an ephemeral per-request dir and cleans it up — within-cascade CF reuse without any operator burden.

---

## Recipe 7 — Force a specific pattern

| You want | Set |
|---|---|
| A/B/C only (no browser) | `"mode": "fetch"` |
| E1 only (Crawl4AI + LLM) | `"mode": "extract"` |
| E2 only (browser-use multi-step) | `"mode": "browse"` |
| Pattern D directly (skip A/B/C ladder) | `"mode": "hostile"` |
| Auto-cascade (default) | (omit `mode`) |

---

## Debugging recipes

### Why did the cascade end at E2?

Read `escalation_log`:

```bash
curl -s -X POST http://localhost:5792/scrape -d '{...}' | jq '.escalation_log'
```

```json
[
  {"step": "a_b_c", "outcome": "failed", "reason": "blocked",
   "duration_s": 0.5, "detail": "all profiles 403"},
  {"step": "d", "outcome": "rejected", "reason": "no_signal",
   "duration_s": 18.2, "detail": "status=200; no LD+JSON / microdata / CSS rows"},
  {"step": "e1", "outcome": "failed", "reason": "blocked",
   "duration_s": 32.1, "detail": "blocked by anti-bot protection"}
]
```

Reads top-to-bottom: A/B/C lost to CF, Pattern D fetched a page but found no extraction signal (likely SPA shell — try a CSS schema), E1 fresh-fought CF and lost (Camoufox fingerprint differs from Scrapling).

### Did `[hostile]` actually load?

```bash
curl -s http://localhost:5792/ready | jq '.checks'
```

Look for `"hostile_installed": true` and `"warnings": []`. If `hostile_installed` is false, the cascade silently skips Pattern D.

### What's my throughput / latency distribution?

```bash
curl -s http://localhost:5792/metrics | grep scrapper_pattern_duration_seconds
```

Histogram buckets per `step` and `outcome`. Plug into Grafana/Prometheus for live dashboards.

---

## Anti-patterns

- **Don't pass `mode="hostile"` for a vendor that doesn't have CF.** You'll waste ~17s on every call. Let the sidecar's auto-cascade pick A/B/C first; D fires only when the ladder is exhausted.
- **Don't enable `pattern_d_network_idle` on server-rendered vendors.** Adds 5-15s for nothing. The auto-SPA detection only fires the network-idle retry when it actually sees an SPA shell.
- **Don't pass both a Pydantic schema and expect Pattern D to extract.** D's CSS extractor only fires for CSS-shaped schemas. Pydantic schemas route to E1 (LLM). If you want both — D's HTML AND LLM extraction — pass a CSS schema for D and let the cascade escalate to E1 only when D's classifier rejects.
- **Don't set `persist_browser_profile_dir` shared across vendors.** Cookie + fingerprint leakage will get the profile flagged by Cloudflare. Per-vendor dirs only.
