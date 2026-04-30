# Open-source web-scraping landscape — 2026-04-30 snapshot

> **Refresh date**: 2026-04-30 · **Next refresh trigger**: a major signal change (a TLS profile burning, a new tool benchmarking better than current adopted stack, an anti-bot vendor shipping a new defence). Successor docs follow `YYYY-MM-DD-landscape.md`; predecessors stay in the repo as historical record — the diff is the audit trail.

This document explains **why** `scrapper-tool` looks the way it does. The four-pattern ladder (A → B → C → D), the `chrome133a → chrome124 → safari → firefox` impersonation chain, and the explicit "do not adopt" list are all defensible against the alternatives surveyed below.

Cited findings are dated to this document's refresh date. If you're reading this 6+ months out, double-check the sources before treating any claim as current.

---

## §1 — TLS-impersonation libraries

When a site fingerprints the TLS handshake (JA3, JA4, Akamai H2 fingerprint), no amount of changing User-Agent strings will save you. The fix is to make the underlying TLS handshake *look* like a real browser. Three contenders in the OSS Python space as of 2026-Q2.

| Library | Mechanism | Maintenance | Adopted? | Notes |
|---|---|---|---|---|
| **`curl_cffi`** ([lexiforest fork][curl-cffi-gh]) | Python binding for `curl-impersonate`; supplies pre-baked TLS fingerprints for chrome116-146, safari17-18, firefox120-135 | Active (lexiforest); regular releases through 2026-Q2; impersonation profiles up to **chrome146/142/136/133a** + `chrome131_android` available [docs][curl-cffi-targets] | ✅ adopted as primary | Used by Pattern A/B/C TLS-sensitive paths via `vendor_client(use_curl_cffi=True)`. The four-profile impersonation ladder ([`scrapper_tool.ladder`](../reference/ladder.md)) is built around it. |
| **`primp`** | Python wrapper around Rust's `reqwest-impersonate` | Less actively maintained; smaller user base | ❌ not adopted | Considered as a `curl_cffi` alternative; `curl_cffi`'s lead in profile coverage and Cloudflare-bypass demonstrations made it not worth the migration cost. |
| **`rnet`** / **`tls_client`** | Custom TLS stacks emulating browser fingerprints | Variable; some libraries dormant for >6 months | ❌ not adopted | Considered briefly. `curl_cffi` provides the same capabilities with stronger active maintenance. |

**Critical signal — Chrome 116+ fingerprinting**: as of 2026-Q1, Cloudflare's challenge engine reliably fingerprints the chrome116-124 profile family. The signal is documented in [`curl_cffi#500`][curl-cffi-500]: setting `impersonate=chrome116+` "consistently triggers a challenge page, while this doesn't occur with safari or firefox impersonation."

This finding is **the load-bearing reason for the ladder's diversification**: instead of pinning a single chrome profile, [`IMPERSONATE_LADDER`](../reference/ladder.md) walks `chrome133a → chrome124 → safari18_0 → firefox135`. When chrome family is identified, the safari/firefox tail picks up the slack.

[curl-cffi-gh]: https://github.com/lexiforest/curl_cffi
[curl-cffi-targets]: https://curl-cffi.readthedocs.io/en/latest/impersonate/targets.html
[curl-cffi-500]: https://github.com/lexiforest/curl_cffi/issues/500

---

## §2 — Browser-stealth tools (Pattern D)

When TLS impersonation alone isn't enough — Cloudflare Turnstile cross-checks Canvas + WebGL + behavioural signals; Akamai Bot Manager's EVA cookie tracks mouse motion — the only path is to drive a real browser. Five 2026 contenders.

| Tool | Approach | Maintenance | Cloudflare Turnstile | Status |
|---|---|---|---|---|
| **Scrapling** ([D4Vinci][scrapling-gh]) | Playwright + custom stealth patches; **auto-Turnstile-solve** as of 2026 [Mintlify guide][scrapling-mintlify] | Active; production references include OpenClaw | **Bypasses** | ✅ **adopted** as Pattern D primary via [`patterns.d.hostile_client`](../patterns/d-hostile.md). `pip install scrapper-tool[hostile]`. |
| **Camoufox** ([daijro][camoufox-gh]) | Custom Firefox build with stealth patches baked in at compile time | Active (daijro) | **0 % detection** per [Scrapewise 2026 benchmark][scrapewise-2026]; **200 MB RAM/instance** | ⏸ candidate; may adopt if Scrapling's Turnstile bypass burns. Higher per-instance memory than Scrapling. |
| **`nodriver`** ([ultrafunkamsterdam][nodriver-gh]) | CDP-free Chrome automation; same author as `undetected-chromedriver` | Active | Mostly bypasses; not always with Turnstile auto-solve | ⏸ candidate; superseded by Scrapling for our use cases. |
| **`patchright`** | Playwright with stealth patches applied at runtime | Active | **Inconsistent** — partial Turnstile failures reported by community 2026-Q1 | ❌ rejected — Scrapling does what we need with auto-solve. |
| **`playwright-extra` + `puppeteer-stealth`** | Stealth-plugin patches on top of Puppeteer/Playwright | **Deprecated 2025-02** [Scrapfly 2026][scrapfly-2026] | Detected reliably by current Cloudflare | ❌ rejected — see [`do-not-adopt.md`](do-not-adopt.md). |

**Why Scrapling beats `puppeteer-stealth` in 2026**: Cloudflare Turnstile in its 2026 form cross-checks **Canvas + WebGL + TLS + behavioural** signals simultaneously. The puppeteer-stealth deprecation in February 2025 marked the inflection point where stealth-patches-on-top-of-vanilla-Playwright stopped working. Tools that survived (Scrapling, Camoufox) ship purpose-built bypasses rather than patches.

**Cost asymmetry codified in `scrapper-tool`**:
- Default install: 0 MB browser overhead.
- `pip install scrapper-tool[hostile]`: ~400 MB Playwright + Chromium (Scrapling).
- Camoufox alternative: ~500 MB Firefox + stealth patches (if/when adopted).

The lib defers to the consumer's cost budget; Pattern D is opt-in by design.

[scrapling-gh]: https://github.com/D4Vinci/Scrapling
[scrapling-mintlify]: https://mintlify.wiki/D4Vinci/Scrapling/guides/cloudflare-turnstile
[camoufox-gh]: https://github.com/daijro/camoufox
[scrapewise-2026]: https://scrapewise.ai/blogs/playwright-stealth-2026
[nodriver-gh]: https://github.com/ultrafunkamsterdam/nodriver
[scrapfly-2026]: https://scrapfly.io/blog/posts/how-to-bypass-cloudflare-anti-scraping

---

## §3 — Anti-bot platforms in 2026

Knowing which platform is between you and the data narrows the toolset enormously. Four major platforms; two minor; one wildcard.

### Major platforms

| Platform | Detection signals | Effective bypass (2026-Q2) | Notes |
|---|---|---|---|
| **Cloudflare Turnstile** | Canvas + WebGL + TLS + behavioural (mouse motion, focus events) cross-check | **Scrapling** auto-solve; **Camoufox** raw fetch; **`curl_cffi` ladder** for non-Turnstile-protected paths | Most common in 2026. Turnstile has a sub-second JS challenge interstitial. |
| **Akamai Bot Manager** | Sensor data submitted via `/akam/...` POST; **EVA cookie** tracks mouse motion across page loads | **Scrapling** with full session replay; `curl_cffi` insufficient (sensor data has to be generated client-side) | Common on enterprise e-commerce. EVA cookie persists across requests, so cookie-jar replay is necessary. |
| **DataDome** | TLS + Canvas + behavioural; CAPTCHA fallback; aggressive IP fingerprinting | **Scrapling**; some success with rotating residential proxies + Camoufox | Used by classifieds, ticket vendors. |
| **PerimeterX (HUMAN)** | Sensor-data POST; aggressive client-side fingerprint | **Scrapling**; `curl_cffi` mostly insufficient | Used by airlines, sneaker drops. |

### Minor / legacy

| Platform | Detection | Bypass |
|---|---|---|
| **Distil Network** | Now part of Imperva; behavioural + TLS | **Scrapling**; `curl_cffi` sometimes works |
| **Imperva (formerly Incapsula)** | TLS + JS challenge | `curl_cffi` ladder usually sufficient; Scrapling fallback |
| **Kasada** | Aggressive obfuscation; client-side challenge | **Scrapling**; rotating residential proxies often required |

### Wildcard

| Platform | Why wildcard |
|---|---|
| **Custom WAF** | Some sites build their own. Detection signals are unpredictable. Recon (DevTools Network tab) is the only diagnostic. |

**Source**: cross-checked from [Scrapfly 2026 anti-Cloudflare troubleshooting][scrapfly-2026] (canonical for Cloudflare detection signals), [Capsolver 2026 Turnstile bypass guide][capsolver-2026] (signals + bypass patterns), and live testing during PartsPilot's Phase 7 vendor adapter sprints.

[capsolver-2026]: https://www.capsolver.com/blog/Cloudflare/bypass-cloudflare-challenge-2025

---

## §4 — LLM-assisted scraping

A new category in 2024-2026 — LLMs that help you scrape sites by inferring selectors, generating extraction schemas, or driving browsers via natural-language goals. Five contenders surveyed.

| Tool | What it does | Cost model | Determinism | Adopted? |
|---|---|---|---|---|
| **Crawl4AI** ([docs][crawl4ai]) | Markdown extraction + LLM-driven schema inference; self-host; LLM-friendly output | Free (self-host); LLM costs separate | Caches generated schemas; you can regenerate | ⏸ deferred — runtime LLM-in-the-path **breaks per-request cost ceilings** (PartsPilot's 12-call-per-conversation cap). May adopt as a **bootstrap-only** tool (extract once, replay deterministically forever). |
| **Firecrawl** ([cnb][firecrawl]) | Managed SaaS; "scrape this URL → markdown / JSON" | Per-request billing (~$83-$333/month at production volumes) | Returns same shape per URL | ❌ rejected — managed SaaS doesn't fit `scrapper-tool`'s self-host posture. |
| **ScrapeGraphAI** ([github][scrapegraphai]) | Self-host LLM-driven scraper; pluggable LLM backend | Free (self-host) + LLM | Lower than Crawl4AI; agent-loop nature | ❌ rejected — runtime LLM not in lib's scope. |
| **AgentQL** ([github][agentql]) | Query language + Playwright integration for AI-driven element lookup | Mixed (free OSS + paid API tier) | Caches queries, but Playwright-bound | ⏸ candidate for future MCP-side tooling (M13). |
| **Stagehand** (Browserbase) | AI agent + cloud browser; finds elements by intent, not selectors | SaaS; per-browser-minute billing | Low — agent re-decides per call | ❌ rejected — managed SaaS. |
| **Reader** (Jina) | "Convert any URL to Markdown for LLM grounding" | Free tier + paid | Stateless | ⏸ candidate for an MCP tool (M13) but not in core lib. |

**The pattern that fits a self-host lib**: **extract once with LLM, replay deterministically forever**. The expensive (LLM) step generates the selectors / schema; the cheap (regex / `selectolax` / `extruct`) step uses them at runtime. `scrapper-tool` ships only the cheap step; the expensive bootstrap step is up to the consumer.

This is why M13's MCP server exposes `recon_classify` and `extract_product` as tools but **does not** wrap any of the LLM-driven scrapers above as runtime dependencies. The lib stays cheap by default.

[crawl4ai]: https://docs.crawl4ai.com
[firecrawl]: https://cnb.cool/aigc/firecrawl/-/tree/002bfdf639baec43166af2b66951b8c95dd78b76
[scrapegraphai]: https://github.com/ScrapeGraphAI/scrapegraph-sdk
[agentql]: https://github.com/tinyfish-io/agentql

---

## §5 — HTML parsing libraries

Less politically charged than anti-bot, but the choice still matters at fetch volume.

| Library | Backend | Speed (relative) | Adopted? |
|---|---|---|---|
| **`selectolax`** (lexbor) | Cython + `lexbor` C HTML parser | **30-40× BeautifulSoup** at our test volumes | ✅ adopted (Pattern C) |
| **`lxml.html`** | libxml2 | ~10× BeautifulSoup; mature | Available (transitive); not the primary path |
| **`parsel`** (Scrapy) | lxml + custom selectors | Fast, but Scrapy-coupled | Not adopted; we don't use Scrapy |
| **BeautifulSoup** | Python parser | Baseline | ❌ rejected — slow at our volumes |

**Source**: [Art of Web Scraping § 6 — Parsing HTML][aows-parsing] cross-checks this hierarchy with benchmarks against representative pages.

[aows-parsing]: https://aows.jpt.sh/parsing/

---

## §6 — Structured-data extraction (Pattern B)

| Library | Coverage | Adopted? |
|---|---|---|
| **`extruct`** (Zyte) | JSON-LD + microdata + RDFa + OpenGraph + Dublin Core, all in one call | ✅ adopted ([`patterns.b`](../patterns/b-embedded-json.md)) |
| **`mf2py`** | microformats only | Available transitively via `extruct` |
| **Custom regex per adapter** | LD+JSON only | ❌ rejected — was the pre-`extruct` pattern in PartsPilot's adapters; reinventing per vendor wasted ~30 lines per adapter |

`extruct(uniform=True)` returns all three syntaxes (JSON-LD / microdata / RDFa) in JSON-LD-shaped lists, so one walker handles them all — see [`patterns.b._walk_for_product`](../patterns/b-embedded-json.md).

---

## §7 — What's missing from the lib (and why)

Things the survey turned up that are deliberately **not** in `scrapper-tool` core:

- **CAPTCHA solvers** (2captcha, capsolver, deathbycaptcha) — out of scope per `do-not-adopt.md`. Legal/ethics framing.
- **Residential proxy networks** (Bright Data Proxies, Oxylabs, Smartproxy) — economics don't pencil at low volume; lib accepts a single static `proxy` kwarg, networks are a consumer concern.
- **Managed scraping SaaS** (Firecrawl, ZenRows, Scrapfly, ScrapingBee) — managed-SaaS billed per page; lib is OSS self-host.
- **LLM-in-the-runtime scrapers** (Crawl4AI runtime mode, ScrapeGraphAI agent loop) — breaks per-request cost ceilings.
- **Synchronous HTTP libraries** (`requests`, `urllib3`) — async-only stack.
- **`BeautifulSoup`** — superseded by `selectolax` at our fetch volumes.

See [`do-not-adopt.md`](do-not-adopt.md) for the canonical list with dates + per-tool rationale.

---

## §8 — Sources cited

All surveyed 2026-04-30 via Perplexity research + direct repo inspection:

1. `curl_cffi` impersonation profile catalogue — https://curl-cffi.readthedocs.io/en/latest/impersonate/targets.html
2. `curl_cffi` issue #500 (chrome116+ disproportionately fingerprinted) — https://github.com/lexiforest/curl_cffi/issues/500
3. Scrapfly 2026 anti-Cloudflare troubleshooting (puppeteer-stealth deprecation 2025-02; current Turnstile detection signals) — https://scrapfly.io/blog/posts/how-to-bypass-cloudflare-anti-scraping
4. Scrapewise 2026 Playwright-stealth comparison (Camoufox 0 % detection / 200 MB RAM benchmark) — https://scrapewise.ai/blogs/playwright-stealth-2026
5. Scrapling Cloudflare Turnstile auto-solve guide — https://mintlify.wiki/D4Vinci/Scrapling/guides/cloudflare-turnstile
6. Capsolver 2026 Cloudflare Turnstile bypass reference — https://www.capsolver.com/blog/Cloudflare/bypass-cloudflare-challenge-2025
7. Crawl4AI vs Firecrawl 2026 comparison — https://crawl4ai.dev (and its in-repo docs)
8. ScrapeGraphAI SDK — https://github.com/ScrapeGraphAI/scrapegraph-sdk
9. AgentQL — https://github.com/tinyfish-io/agentql
10. Browserbase Stagehand reference (cloud browser API + AI agent) — https://www.browserbase.com/blog/best-web-scraping-tools
11. Firecrawl scraping platform — https://cnb.cool/aigc/firecrawl
12. Reader (Jina) markdown converter — https://jina.ai/reader/
13. Art of Web Scraping § 6 — Parsing HTML (lxml / selectolax / parsel / BeautifulSoup benchmarks) — https://aows.jpt.sh/parsing/
14. `extruct` library (Zyte) — https://github.com/scrapinghub/extruct
15. PyPI Trusted Publisher (OIDC tag-triggered release) — https://docs.pypi.org/trusted-publishers/
16. MCP Python SDK — https://pypi.org/project/mcp-agent/
17. Playwright MCP (Microsoft) — https://playwright.dev/python/agents
18. OpenClaw vs Hermes Agent comparison — https://petronellatech.com/blog/openclaw-vs-hermes-agent-2026/
19. Composio's Hermes/ScrapingBee integration (manifest-based plugin pattern) — https://composio.dev/toolkits/scrapingbee/framework/hermes-agent

---

## Refresh policy

This document is **append-only history**. When a major signal changes, write a new dated landscape doc (e.g. `2026-Q3-landscape.md`) — don't edit this one. The diff between successive landscape docs is the scraping ecosystem's audit trail.

**Triggers for a new dated landscape**:
- A `curl_cffi` impersonation profile burns and the ladder needs reordering.
- A new browser-stealth tool benchmarks better than Camoufox / Scrapling.
- A new anti-bot platform gains material market share.
- A new structured-data syntax (post-RDFa, post-JSON-LD) becomes commonplace.
- An OSS scraping library we depend on goes unmaintained for >6 months.
