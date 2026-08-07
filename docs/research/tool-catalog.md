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
| `rookiepy` | cookie extraction (host-side) | Active; MIT; Rust/pyo3 | ~3 MB wheel | ✅ from 2026-08 via the `[cookies]` extra. Wheels are version-specific up to `cp312` (not abi3) — 3.13/3.14 build from sdist and need a Rust toolchain, which is why it is not in the CI matrix. |
| `browser_cookie3` | cookie extraction (host-side) | Active | n/a | ❌ **never declared — LGPL.** Used only if a user already has it, discovered via `find_spec`. A test asserts it appears in no dependency list and no lockfile. |
| Agent-Reach | — (installer/router for other CLIs) | Active | n/a | ❌ rejected as an engine 2026-08-01 — its universal web path is one `urllib` GET to `r.jina.ai`, strictly weaker than tier 1. Two of its *ideas* were adopted (cookie extraction, `doctor`), built from scratch. |
| `Scrapegraph-ai` (OSS) | E1 alternative | Active | LangChain-heavy | ❌ rejected 2026-08-01 — `FetchNode` is a plain Playwright loader with no anti-bot; anti-bot is their paid tier. The 2026-04 survey evaluated the wrong repo (`scrapegraph-sdk`). |

See [`do-not-adopt.md`](do-not-adopt.md) for the full reject list with reasons + dates.
