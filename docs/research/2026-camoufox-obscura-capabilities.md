# Camoufox & Obscura — capabilities, configuration, and how to leverage them

Deep-dive reference for the two stealth-browser backends behind Pattern E, with a
version check and concrete leverage points mapped to `scrapper-tool`'s code.

_Last researched: 2026-07 (Camoufox pip 0.5.4, Obscura 0.1.10)._

## Version status — are we current?

| Backend | We use | Latest | Status | Notes |
|---|---|---|---|---|
| **Camoufox (pip)** | `0.4.11` | **`0.5.4`** | **BEHIND** | Upgrade recommended. 0.5.3+ needs Python ≥3.10 (we're on 3.12/3.13 ✓). The 0.5.x wheel grew a lot (~71 MB → ~1.4 GB on some platforms) — validate image size after upgrade. `fingerprint_preset` (real bundled fingerprints) is a 0.5-era feature worth adopting. |
| **Camoufox (browser binary)** | `152.0.4-beta.28` | `152.0.4-beta.28` | **CURRENT** | Fetched via `camoufox fetch`; matches latest Firefox-fork build. |
| **Obscura** | `0.1.10` (Docker Hub image) | `0.1.10` | **CURRENT** | BUT: the prebuilt Hub image is almost certainly **not** compiled with `--features stealth`, so `--stealth` gives only fingerprint consistency — **no TLS impersonation / tracker blocking**. This is the likely reason it failed Yad2's Radware. See "leverage" below. |

---

## Camoufox

### What it is
A Firefox fork that patches the engine at the **C++ level** (not JS injection), so
spoofing is invisible to JavaScript inspection. Covers navigator, WebGL, fonts,
geolocation, timezone, WebRTC, audio, screen. Fingerprints are drawn from
BrowserForge's real-world statistical distributions. Highest open-source bypass
rate (~0% headless detection on 2026 benchmarks). ~200 MB RAM/instance. Driven as a
standard Playwright browser via `camoufox.async_api.AsyncCamoufox`.

### Full configuration reference (`AsyncCamoufox(**opts)`)

| Option | Type | What it does |
|---|---|---|
| `headless` | `bool \| "virtual"` | `"virtual"` = Xvfb virtual display (Linux) — **better stealth than pure headless**. |
| `humanize` | `bool \| float` | Human-like cursor movement. Float = max seconds per movement (default ~1.5 s). |
| `geoip` | `bool \| str` | Spoof geolocation/timezone/locale from proxy IP (needs `camoufox[geoip]`). `True` = auto. |
| `locale` | `str \| list` | Locale / country code(s). |
| `proxy` | `dict` | `{"server": ..., "username": ..., "password": ...}`. |
| `os` | `str \| list` | `"windows"`/`"macos"`/`"linux"` (or list → random). Default: random of all three. |
| `screen` | `Screen(...)` | Constrain dimensions, e.g. `Screen(max_width=1920, max_height=1080)`. |
| `window` | `(w, h)` | Fixed window size (⚠ fixed sizes are themselves fingerprintable). |
| `fonts` | `list[str]` | Extra system font families to load. |
| `fingerprint_preset` | `bool \| dict` | `True` = random real bundled fingerprint (0.5-era; more realistic than generated). |
| `webgl_config` | `(vendor, renderer)` | Pin a specific GPU vendor/renderer (must match the OS). |
| `config` | `dict` | Low-level override of individual fingerprint props: `navigator`, `screen`, `window`, `webGl`, `fonts`, `geolocation`, `timezone`. |
| `block_images` | `bool` | Drop all image requests — **big speed win** for text extraction. |
| `block_webrtc` | `bool` | Kill WebRTC (prevents IP leak behind proxy). |
| `block_webgl` | `bool` | Disable WebGL (some detectors flag it; also faster). |
| `disable_coop` | `bool` | Disable Cross-Origin-Opener-Policy (cross-origin iframe access). |
| `enable_cache` | `bool` | Default `False`; enable to allow `page.go_back()`/`go_forward()`. |
| `addons` | `list[str]` | Paths to extracted addon folders (e.g. uBlock Origin → ad/tracker blocking). |
| `exclude_addons` | `list[DefaultAddons]` | Disable bundled default addons. |
| `persistent_context` | `bool` | Persist a profile — **requires `user_data_dir`**. |
| `user_data_dir` | `str` | On-disk profile dir (cookies incl. `cf_clearance` survive between launches). |
| `main_world_eval` | `bool` | Allow main-world script injection (`mw:` prefix). |
| `i_know_what_im_doing` | `bool` | Bypass safety checks. |

**CLI:** `camoufox fetch` (download binary) · `camoufox path` · `camoufox sync` ·
`camoufox set <version>` (pin browser build).

### How scrapper-tool uses it today
`CamoufoxBackend.launch` (`src/scrapper_tool/agent/backends/browser.py`) passes only:
```python
{"headless": not headful, "humanize": True, "geoip": True}  # + proxy if set
```
That's **4 of ~20 knobs**. Everything else runs on defaults.

### Leverage opportunities (mapped to our code)

1. **Upgrade to 0.5.4** (behind Phase 1 pin/test-net). Adopt `fingerprint_preset=True`
   for real (not generated) fingerprints — a likely bypass improvement on hard sites.
2. **`headless="virtual"` in Docker.** Our image already installs `xvfb`. Virtual-display
   mode is meaningfully stealthier than pure headless and may help against exactly the
   behavioral walls (Radware/DataDome) that beat us on Yad2. Low-risk, high-upside.
3. **Fix the `user_data_dir` gap (real bug).** `AgentConfig.user_data_dir` is plumbed
   through the cascade for `cf_clearance` carry-forward, but `CamoufoxBackend.launch`
   **never passes it to Camoufox** — so E2 Camoufox sessions don't persist cookies. Pass
   `user_data_dir` + `persistent_context=True` when `config.user_data_dir` is set.
4. **`block_images=True` for extraction runs** — Yad2's page was 8.5 MB; blocking images
   cuts render time/bandwidth substantially when we only need text/DOM.
5. **`block_webrtc=True` when using a proxy** — closes the WebRTC IP-leak that defeats
   proxy anonymity.
6. **Expose `os` / `screen` / `locale` via config** so callers can match the target's
   audience (e.g. `locale="he-IL"`, `os="windows"` for Israeli sites like Yad2).
7. **`addons=[uBlock]`** for ad/tracker blocking → faster loads + fewer detection scripts.
8. **`humanize=<float>`** to tune cursor timing per target instead of the default.

---

## Obscura

### What it is
A ~30 MB Rust headless browser (embedded V8) exposing a Chrome DevTools Protocol
server. Apache-2.0. Fast (~85 ms loads), tiny, but younger and less battle-tested on
stealth than Camoufox. Four subcommands: `fetch`, `serve`, `scrape`, `mcp`.

### CLI capabilities

**`obscura serve`** — CDP WebSocket server (what our backend connects to):
`--host` (default 127.0.0.1 — **set `0.0.0.0` in containers**), `--port` (9222),
`--workers`, `--proxy`, `--stealth`, `--obey-robots`. Playwright/Puppeteer connect at
`http://host:9222` (Playwright discovers the ws endpoint) or
`ws://host:9222/devtools/browser`.

**`obscura fetch <url>`** — one-shot render:
`--dump html|text|links|markdown|assets|original` (**built-in DOM→markdown!**),
`--eval <js>`, `--wait-until load|domcontentloaded|networkidle0`, `--selector`,
`--stealth`, `--output`, `--proxy`.

**`obscura scrape <urls...>`** — parallel multi-URL: `--concurrency` (10),
`--eval`, `--format json|text`. (Requires `obscura` + `obscura-worker` co-located.)

**`obscura mcp`** — MCP server (stdio or `--http --port`), tools:
`browser_navigate/snapshot/click/fill/type/press_key/select_option/evaluate/
wait_for/network_requests/console_messages/close`.

**Global:** `--user-agent`, `--storage-dir` (session persistence), `--v8-flags`
(e.g. `--max-old-space-size=4096`), `--allow-private-network`.

**Env:** `OBSCURA_SCRIPT_DEADLINE_MS` (script budget, ~30 s default — raise for heavy
SPAs), `OBSCURA_NETWORK_BODY_BUFFER_BYTES` (response cache, 2 MiB default),
`OBSCURA_ALLOW_PRIVATE_NETWORK`.

**CDP domains implemented:** Target, Page, Runtime, DOM, Network (setCookies /
setExtraHTTPHeaders / setUserAgentOverride), **Fetch (request interception:
continue/fulfill/fail)**, IO (stream large bodies), Storage, Input, and a custom
`LP.getMarkdown`.

### Measured: the stealth build, benchmarked (2026-07)

`Dockerfile.obscura` builds obscura from source with `--features stealth`. Result:
a **247 MB** image (much smaller than the app image) reporting version `0.1.0` —
the `main` branch's Cargo version, i.e. *not* the same tag as the Hub release
`0.1.10`; worth pinning `OBSCURA_REF` if reproducibility matters. Unlike the Hub
image it answers `GET /json/version`, and Playwright `connect_over_cdp` works
against it through our render tier.

`scripts/e2e/bench_obscura.py` (render tier, 6 s settle, same machine/IP):

| Target | Camoufox | Obscura-stealth |
|---|---|---|
| `quotes.toscrape.com/js/` (JS-only control) | PASSED, 8940 B, **12.6 s** | PASSED, 8940 B, **8.6 s** |
| Yad2 (Radware/ShieldSquare) | CHALLENGED (18 KB perfdrive page) | CHALLENGED (118 KB Radware loader) |

Reading it honestly:
- **Obscura-stealth works and is ~30-40% faster** than Camoufox on a clean JS
  target — its speed/RAM claims hold up.
- The Yad2 row is **inconclusive, not a verdict on obscura**: by the time this ran,
  the test IP had been flagged by repeated automated hits, and Camoufox — which
  passed the same URL cleanly earlier the same day — was *also* challenged.
  Anti-bot vendors escalate on IP reputation, so a stealth comparison on a hard
  target is only meaningful from a clean IP / rotating proxy pool. That is exactly
  the gap the proxy-rotation task addresses; re-run this benchmark then.
- Do **not** promote obscura in any auto-selection heuristic on this data. It is
  established as fast and functional; its stealth vs Camoufox is still unmeasured.

### The stealth-build caveat (important)
`--stealth` help: *"consistent browser fingerprint, and **with the `stealth` build
feature**, TLS impersonation plus tracker blocking"*. Full stealth (per-session
GPU/screen/canvas/audio/battery randomization, `navigator.webdriver=undefined`, 3,520
blocked tracker domains, **TLS/JA3 impersonation**) requires
`cargo build --release --features stealth`. The **Docker Hub `h4ckf0r0day/obscura:latest`
image is not that build**, so runtime `--stealth` only gives fingerprint consistency —
no TLS impersonation. That matches our result: obscura got Yad2's Radware loader page
while Camoufox passed silently.

### How scrapper-tool uses it today
`ObscuraBackend.launch` does `connect_over_cdp(http://…:9222)` and returns the
Playwright browser. We use only the `serve` CDP path — none of `fetch`/`scrape`/`mcp`.

### Leverage opportunities
1. **Build/obtain the `--features stealth` image** to unlock TLS impersonation +
   tracker blocking — without it, obscura's anti-bot value is limited. Benchmark the
   stealth build via the `canary` CLI before promoting it.
2. **`obscura fetch --dump markdown` as an E1-lite path.** It renders + returns clean
   markdown in one call with no Python browser stack — a cheap alternative to Crawl4AI
   for lightly-protected pages (feed the markdown straight to the LLM).
3. **`obscura scrape --concurrency N`** is a ready-made **parallel batch** primitive —
   relevant to the deferred crawl/map follow-up.
4. **CDP `Fetch` interception** — block images/ads/trackers at the protocol level for
   faster E2 renders through obscura.
5. **Tuning:** raise `OBSCURA_SCRIPT_DEADLINE_MS` for JS-heavy SPAs; `--v8-flags
   --max-old-space-size` for big pages.
6. Recall the **browser-use↔Firefox constraint** (see `2026-e2-agent-framework.md`):
   obscura is Chromium/CDP, so it gives browser-use **full action support** (scrolling
   etc.) that Camoufox/Firefox can't — the trade is obscura's weaker stealth. A
   stealth-compiled obscura could become the preferred **E2** backend.

---

## Recommended actions (prioritized)

1. **Upgrade Camoufox `0.4.11 → 0.5.4`** (with the Phase 1 pin + test-net), adopt
   `fingerprint_preset=True`. Validate Docker image size.
2. **Pass `user_data_dir` + `persistent_context` to `CamoufoxBackend`** — fixes the
   silent `cf_clearance` carry-forward gap for E2.
3. **Add `headless="virtual"` (Xvfb) and `block_images` options** to `CamoufoxBackend`;
   default virtual-display in the container for better stealth.
4. **Expose Camoufox `os`/`screen`/`locale`/`block_webrtc`/`addons`** via `AgentConfig`
   so callers can match the target and speed up loads.
5. **Produce a `--features stealth` Obscura image** (or find a stealth-tagged release)
   and benchmark it with `canary`; only then consider obscura for hard targets.
6. **Prototype `obscura fetch --dump markdown`** as a fast, dependency-light E1 variant
   and `obscura scrape` for the deferred batch/crawl primitive.
