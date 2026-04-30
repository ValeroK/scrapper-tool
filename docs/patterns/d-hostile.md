# Pattern D — Hostile

> *Stub — populated in M5.*

**Signals**: anti-bot platform (Cloudflare Turnstile, Akamai EVA / Bot Manager, DataDome, PerimeterX, Distil, Kasada) blocks both default `httpx` and `curl_cffi` (chrome133a + ladder fallbacks).

**Helper**: `scrapper_tool.patterns.d.hostile_client()` — a [Scrapling](https://github.com/D4Vinci/Scrapling)-backed async context manager with auto-Turnstile-solve. Requires the `[hostile]` extra:

```bash
pip install scrapper-tool[hostile]
```

**Cost**: highest. Scrapling pulls Playwright + Chromium (~400 MB image bloat, materially slower per request). Reserve for vendors that genuinely won't yield to Patterns A/B/C.

See [docs/research/2026-04-30-landscape.md](../research/2026-04-30-landscape.md) for why Scrapling is the chosen Pattern D tool over Camoufox / nodriver / patchright as of 2026-04-30.
