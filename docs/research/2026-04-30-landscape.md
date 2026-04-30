# Open-source web-scraping landscape — 2026-04-30 snapshot

> *Stub for M0. Filled in during M5.5 with the full Perplexity-sourced research dossier covering TLS-impersonation libs, browser-stealth tools, anti-bot platform behaviour, and LLM-assisted scraping tooling.*
>
> Until then, the source-of-truth is PartsPilot's [`scraping-vendor-recon` skill](https://github.com/ValeroK/affiliate-service/blob/main/.claude/skills/scraping-vendor-recon/SKILL.md) and the per-vendor `adapter_notes.md` journals at `src/partspilot/vendors/*/adapter_notes.md`.

## Sections planned for M5.5

1. **TLS-impersonation libraries** — `curl_cffi` (active, lexiforest fork; profiles up to chrome146/142/136/133a + safari18/firefox135) vs `hrequests` / `primp` / `rnet` / `tls_client`.
2. **Browser-stealth tools** — Scrapling (auto-Turnstile-solve, OpenClaw production reference); Camoufox (Firefox-stealth, 0% Cloudflare detection per Scrapewise 2026 benchmark, 200 MB RAM); nodriver (CDP-free, undetected-chromedriver author); patchright (partial Turnstile failures); playwright-extra + puppeteer-stealth (deprecated 2025-02).
3. **Anti-bot platforms** — Cloudflare Turnstile (canvas + WebGL + TLS + behavioural); Akamai Bot Manager (sensor-data + EVA cookie); DataDome / PerimeterX / Imperva / Distil / Kasada.
4. **LLM-assisted scraping** — Crawl4AI / Firecrawl / ScrapeGraphAI / AgentQL / Stagehand / Reader (Jina) — selector generators vs runtime browser agents vs markdown converters; the "extract once with LLM, replay deterministically forever" pattern.

## Sources used

- `curl_cffi` impersonation profile list — https://curl-cffi.readthedocs.io/en/latest/impersonate/targets.html
- `curl_cffi` issue #500 (Chrome 116+ disproportionately fingerprinted) — https://github.com/lexiforest/curl_cffi/issues/500
- Scrapfly 2026 anti-Cloudflare troubleshooting (puppeteer-stealth deprecation) — https://scrapfly.io/blog/posts/how-to-bypass-cloudflare-anti-scraping
- Scrapewise 2026 Playwright-stealth comparison — https://scrapewise.ai/blogs/playwright-stealth-2026
- Scrapling Cloudflare Turnstile auto-solve guide — https://mintlify.wiki/D4Vinci/Scrapling/guides/cloudflare-turnstile

## Refresh cadence

This document is dated. Refresh trigger: a major signal change (a TLS profile burning, a new tool benchmarking better than Scrapling/Camoufox, an anti-bot vendor shipping a new defence). Successor doc names follow `YYYY-MM-DD-landscape.md`; predecessors stay in the repo as historical record (the diff between them is the audit trail).
