# Tool catalog (living matrix)

> *Stub for M0. Filled in during M5.5.*

| Tool | Pattern fit | Maintenance (2026-04-30) | Image-bloat cost | Status |
|---|---|---|---|---|
| `httpx` | A, B (light), C | Active (encode org) | 0 MB extra | ✅ default |
| `curl_cffi` | A, B (TLS-sensitive) | Active (lexiforest) | ~15-20 MB | ✅ via `use_curl_cffi=True` |
| `selectolax` (lexbor) | C | Active | ~5 MB | ✅ default HTML parser |
| `extruct` (Zyte) | B (JSON-LD/microdata/RDFa) | Active | ~3 MB + transitives | ✅ from M3 |
| Scrapling | D (Cloudflare Turnstile + Akamai EVA) | Active (D4Vinci); auto-Turnstile-solve as of 2026 | ~400 MB (Playwright) | ✅ on-shelf via `patterns.d.hostile_client()` |
| Camoufox | D (Firefox-stealth fallback if Scrapling burns) | Active (daijro); 0% Cloudflare detection per Scrapewise 2026; 200 MB RAM/instance | ~500 MB | ⏸ candidate; not adopted yet |
| nodriver | D (CDP-free Chrome) | Active (ultrafunkamsterdam) | ~400 MB | ⏸ candidate; superseded by Scrapling for our use-cases |
| patchright | D (Playwright-stealth patches) | Active but not 100% Turnstile-effective per 2026 reports | ~400 MB | ❌ rejected — Scrapling does what we need with auto-solve |

See [`do-not-adopt.md`](do-not-adopt.md) for the full reject list with reasons + dates.
