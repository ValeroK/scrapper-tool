# Do not adopt

Append-only list of rejected tools / approaches with date + reason.

**Rule**: overturning a reject (e.g. a previously-deprecated tool gets revived under new maintenance) requires a *new* dated entry, never editing the old one. The diff is the audit trail.

---

### `puppeteer-stealth` / `playwright-extra` (rejected 2026-04-30)

Maintainers deprecated puppeteer-stealth in February 2025; playwright-extra is stale in Node.js. Current Cloudflare Turnstile detects the patches. Source: [Scrapfly 2026 anti-Cloudflare troubleshooting](https://scrapfly.io/blog/posts/how-to-bypass-cloudflare-anti-scraping).

### Crawl4AI runtime mode (rejected 2026-04-30)

Runs an LLM in the request path. Breaks per-request cost ceilings for any consumer with a budget invariant (e.g. PartsPilot's 12-call-per-conversation cap). LLM-assisted *bootstrapping* of selectors (extract-once, replay-deterministically-forever) may land as a separate skill — but not in the runtime.

### Firecrawl / ZenRows / Bright Data Scraping Browser / Scrapfly / ScrapingBee / Oxylabs Web Unlocker (rejected 2026-04-30)

Managed-SaaS billed per page. `scrapper-tool` is open-source self-host. Consumers are free to wrap a SaaS themselves if their economics demand it, but it's not bundled.

### 2captcha / capsolver / deathbycaptcha (rejected 2026-04-30)

Out of scope per founder direction. Legal/ethics framing for consumer-facing affiliate-revenue use cases (PartsPilot's primary downstream consumer).

### Residential proxy networks — Bright Data Proxies / Oxylabs / Smartproxy (rejected 2026-04-30)

Economics don't pencil at low volume (PartsPilot baseline: 1k conversations/month). The lib supports a single static `proxy` kwarg; full networks are a consumer concern.

### `requests` library (rejected 2026-04-30)

Synchronous; doesn't fit the async stack the lib is built around (`httpx.AsyncClient` / `curl_cffi.AsyncSession`).

### `BeautifulSoup` (rejected 2026-04-30)

30-40× slower than `selectolax` (lexbor backend) on the parsing benchmarks consumers care about. `BeautifulSoup` remains the pedagogical default for first-time scrapers but isn't a fit for production fetch volumes.
