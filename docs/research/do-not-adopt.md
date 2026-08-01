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

---

## Overturned rejects

Per the rule at the top of this file: a reject is overturned by a **new dated
entry**, never by editing the original. The originals above stand as written.

### Crawl4AI runtime mode — reject OVERTURNED 2026-08-01

Rejected 2026-04-30 on the grounds that it "runs an LLM in the request path;
breaks per-request cost ceilings." We then shipped exactly that as **Pattern E1
in v1.0.0**, and this file was never updated — so for three releases the
canonical do-not-adopt list contradicted the shipped product. Recording it now.

What changed is the cost premise, not the architecture. The 2026-04 survey
assumed a metered API sat behind every LLM call, which is what made an LLM in
the request path incompatible with a per-request budget invariant. Pattern E1
runs against a **local Ollama** by default, where the marginal cost of a call is
latency rather than money. The objection was about billing, and local inference
removed the billing.

The determinism objection was answered separately, and better, by the recipe
layer: E1's expensive win is distilled into a CSS recipe that replays for free
on the next page of the same shape. That is the "extract once, replay
deterministically forever" pattern the original entry itself named as the
acceptable shape — it just arrived as a layer *around* the LLM tier instead of
as a substitute for it.

Still true, and still worth keeping from the original reject: E1 is not the
default path. It sits behind replay, A/B/C, Pattern D and the stealth render
tier, and is reached only when all of them fail.

### ScrapeGraphAI — reject UPHELD 2026-08-01, on corrected grounds

The 2026-04-30 evaluation **assessed the wrong artifact.** The landscape table
rejects "ScrapeGraphAI" but every link points at
[`scrapegraph-sdk`](https://github.com/ScrapeGraphAI/scrapegraph-sdk) — the
*managed cloud SDK* — while the thing worth evaluating is
[`Scrapegraph-ai`](https://github.com/ScrapeGraphAI/Scrapegraph-ai), the
self-hosted OSS library. Two different artifacts with different cost models, so
the stated reason ("managed SaaS" / "runtime LLM not in scope") was attached to
the wrong one.

Re-evaluated 2026-07 against the OSS library, the verdict is unchanged but the
reasoning is now accurate:

- Its `FetchNode` is a plain Playwright `ChromiumLoader` with a BeautifulSoup
  fallback — **no anti-bot handling, no challenge detection, no node-level
  retries.**
- Its own README scopes the OSS tier as "you bring your own LLM keys and manage
  browser/proxy configuration," while the paid cloud API is what adds "anti-bot
  protection."

That last point is the whole argument. **Anti-bot is their monetized tier, and
it is the layer we give away with measured evidence behind it.** Adopting the
OSS library would replace Crawl4AI in the E1 slot, bring no anti-bot capability,
and add a LangChain-heavy dependency tree — a lateral move at best.

Two things from it are worth keeping in view, recorded so they are not lost:

- **`SearchGraph`'s entry point** — scraping seeded by a *query* rather than a
  URL. Our `map_site` / `crawl_site` are both seed-URL-driven, so query-seeded
  discovery is a genuine gap. It needs a search backend, so it is a proposal
  with a cost rather than a freebie. Its own roadmap item, not a dependency.
- **`FetchNode` accepts `storage_state`** — useful evidence that `storage_state`
  is the industry-standard shape for authenticated LLM scraping. A data point
  supporting our own cookie work, not a reason to reverse the deferral of a
  `storage_state` request field: no tier of ours reads `localStorage`, which is
  that shape's only unique payload.

**Explicitly not taken:** `ScriptCreatorGraph`, which generates Python scrapers.
`recipe/derive.py` already solves that problem better — deterministic selectors
replayed for free, with no code-execution surface and no silent drift when a
generated scraper stops matching the page.

### Agent-Reach — evaluated and rejected as an engine 2026-08-01

Reviewed against this cascade to test the claim that it "scrapes any website
without issues." It does not, and does not claim to: Agent-Reach is an
**installer / router / health-checker** for other people's CLI tools. Its
universal web path is a single `urllib` GET against `https://r.jina.ai/<url>`
(`agent_reach/channels/web.py`) — no retry, no rendering, no challenge handling,
strictly weaker than our tier 1. Its README carries a "Capability Boundary"
section disclaiming logged-in pages, and never mentions Cloudflare, DataDome or
Akamai.

Not adopted as an engine. **Two of its ideas were adopted**, because they
plugged real holes rather than duplicating anything: browser cookie extraction
(we had no way to get an authenticated session into the cascade) and a `doctor`
command (we had no way to ask whether an install was functional). Both shipped
in this branch, built from scratch against our own architecture rather than
vendored.

