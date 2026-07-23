# Changelog

All notable changes to `scrapper-tool` are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **Stealth-render cascade tier (no LLM).** `/scrape` and MCP `auto_scrape` now
  try a stealth-browser render plus the existing deterministic extractors
  between Pattern D and the LLM tiers, reported as `pattern_used="render"` with
  `tokens_used=0`. On by default (`SCRAPPER_TOOL_RENDER_TIER=0` disables);
  skipped without an entry in `pattern_attempts` when `[llm-agent]` is absent.
  Measured motivation: one target returned 403 on all four TLS profiles while
  rendering 1.35 MB of genuine content, and another went from 4 extractable
  headlines to 212 once rendered — both then extractable with zero tokens, so
  the LLM is now genuinely the last resort rather than the first escalation.
  Success is judged on extracted content, not status code, so a 403 carrying a
  real rendered DOM is a win. The cascade-resolved profile directory is shared
  with the browser, carrying clearance cookies forward from earlier rungs, and
  the rendered DOM becomes `intermediate_raw_text` when the LLM tier still runs.
- **Learn-once / replay (`pattern_used="replay"`).** When an expensive tier
  succeeds, the cascade derives the CSS selectors that would have produced the
  same data for free, verifies them against that page, and caches the recipe per
  domain. The next request for that domain replays it — a fetch plus a
  selectolax parse instead of a browser launch or an LLM call. On by default
  (`SCRAPPER_TOOL_RECIPE_CACHE=0` disables; `SCRAPPER_TOOL_RECIPE_DIR` relocates
  the cache). Wired into both REST `/scrape` and MCP `auto_scrape`.
  - Recipes carry the tier they were learned from, so a render-learned recipe is
    replayed with a render rather than silently returning nothing over a raw
    fetch — and when its selectors provably also match the body A/B/C already
    fetched, it is downgraded to a fetch recipe so future replays skip the
    browser entirely.
  - Drift self-heals: a recipe that stops matching is evicted and the normal
    cascade re-derives a fresh one.
  - No recipe is derived for JSON-LD/microdata wins (Pattern B already handles
    those deterministically) or for pages whose only handles are build-generated
    class hashes. Refusing costs one full-price request; a wrong recipe would
    cost correctness.
- **Challenge detection now steers escalation, and is reported.** Every
  `/scrape` and `auto_scrape` response carries `challenge_detected` — the bot
  vendor that walled us (`cloudflare`, `radware`, `datadome`, `perimeterx`,
  `akamai`, `kasada`, `incapsula`, `unknown`) or null. Previously the
  heuristics were private to `http_server`, Cloudflare-only, and used solely to
  pick a Scrapling retry strategy; MCP had none at all. On a non-Cloudflare
  wall the cascade now skips Pattern D and goes straight to the render tier,
  because Scrapling's only anti-bot weapon (`solve_cloudflare`) doesn't apply
  and D would spend a browser launch re-fetching the same interstitial. A
  Cloudflare wall still runs D — that is what it is for.
- **Obscura browser backend (experimental).** `browser="obscura"` connects to
  an external Obscura CDP server (`obscura serve`) via Playwright
  `connect_over_cdp`, returning a real Playwright browser that drives E2
  (`agent_browse`) directly. Lightweight (~30 MB RAM) alternative to Camoufox.
  Configured via `SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL` (default
  `ws://127.0.0.1:9222`). Non-default and benchmark-gated — measure its real
  detection rate via the `canary` CLI before trusting it on protected sites. A
  profile-gated `obscura` service is included in `docker-compose.yml`.

### Changed
- **BREAKING (cascade behaviour): E2 no longer runs automatically.** A blocked
  E1 used to auto-escalate into the browser-use agent loop on every `mode=auto`
  scrape. E2 is the most expensive tier by a wide margin and the only thing it
  can do that the render tier can't is *interact* — a page that is simply walled
  will wall the agent too, just slower and for many more tokens. Pass
  `interactive: true` (REST `/scrape`) or `interactive=True` (MCP
  `auto_scrape`) for targets that genuinely need login / pagination / dynamic
  forms. Otherwise the cascade stops at E1 and returns E1's blocked result,
  which carries the escalation log and any partial content. An explicit REST
  `mode="browse"` is a direct request for E2 and is never gated. To restore the
  old behaviour, set `interactive: true` on the calls that relied on it.
- **Captcha solver cascade is now actually wired into Pattern E.** Previously
  `get_captcha_solver(config)` was constructed and discarded (`_ =`) in both E1
  and E2, so `.solve()` never ran — only Camoufox's built-in `humanize` defeated
  Turnstile. The solver now runs on the live page via a browser-use
  `on_step_end` hook (E2) and a Crawl4AI `after_goto` hook (E1), through a new
  mechanism-aware DOM helper (`captcha_dom`): stealth auto-pass first, in-context
  handling preferred for Turnstile, foreign-token injection only for
  portable-token kinds (reCAPTCHA / hCaptcha).
- **Behavior policy is now wired in** the same way (page-level settle/scroll
  shaping in E2; a minimal pre-return settle in E1). Previously discarded.
- **MCP `auto_scrape` now uses the same accept logic as REST `/scrape`.** The
  two surfaces shared a cascade in name only — MCP always escalated to an LLM
  call whenever a schema was supplied, even when Pattern B/C/D produced a valid
  signal. Both now delegate to one shared `classify_extraction_success`
  (extracted from REST's classifier; REST behavior is unchanged).

### Removed
- **Zendriver and Botasaurus browser backends** (and their `zendriver-backend` /
  `botasaurus-backend` extras). Both returned `playwright_browser=None`, so
  `agent_browse` raised `AgentError` at runtime for them — they were never
  drivable via Pattern E. The lightweight/CDP niche they aimed at is now filled
  by the Obscura backend.

## [1.4.2] - 2026-05-06

Hotfix release. v1.4.0/v1.4.1's extractor registry bootstrap had a
short-circuit bug — when one of the built-in extractor modules was
imported before the first ``get()`` call (which happens in normal
flow because ``css.py`` is imported via ``looks_like_css_schema``),
the ``if not _REGISTRY`` check skipped registering the other three
built-ins. Result: every D-step ``/scrape`` call raised
``KeyError: Unknown extractor 'json_ld_product'`` and surfaced as
``SidecarBackendDown`` to the affiliate.

### Fixed

- ``_extractors.get()`` and ``_extractors.all_names()`` now bootstrap
  unconditionally. Python's module cache makes the imports a near-no-op
  on the second+ call, so the previous early-return was an over-zealous
  optimization that lost correctness.

## [1.4.1] - 2026-05-06

Hotfix release. v1.4.0's ``solve_cloudflare="auto"`` default did a
probe-first prelude even for ``mode="hostile"`` callers — recon-pinned
hostile vendors (Amayama, Megazip, Tasca) wasted ~3s on the prelude
probe and risked Scrapling's internal CF-retry loop firing twice
(observed pushing per-call latency past the 200s sidecar timeout).

### Fixed

- When ``mode="hostile"`` is set, ``solve_cloudflare="auto"`` now
  resolves to ``True`` (skip the probe; solve straight away). This
  preserves the v1.3.0 cost story for hostile-pinned adapters while
  keeping the probe-first auto behavior for ``mode="auto"`` callers.
- Live smoke against PartsPilot's Amayama probe set: pre-1.4.1 the
  cascade timed out at 200s with `SidecarBackendDown`; post-1.4.1
  back to ~17s + structured Product (matches v1.3.0 baseline).

## [1.4.0] - 2026-05-06

SPA-vendor unblock + observability + the simple-by-default API. Closes the
gap between "Pattern D defeats Cloudflare" and "extraction returns rows"
for vendors whose result pages render in HTML cards rather than
schema.org/Product LD+JSON. Adds smart defaults so most callers stop
needing per-vendor flag combos. Adds `escalation_log` + `/metrics` for
debugging + ops. Backward-compatible — every flag from v1.3.0 keeps
working.

The deliberate split this release introduces:

- **Simple path** (95% of callers): `POST /scrape {"url": "..."}` or
  `{"url": "...", "schema_json": {...}}`. Sidecar picks the right pattern.
- **Controllable path** (adapters with vendor recon): explicit `mode`,
  `solve_cloudflare`, `pattern_d_network_idle`, `persist_browser_profile_dir`
  fields work as before.

### Added

- **CSS-schema extraction inside Pattern D**. When `schema_json` is
  shaped like `{"baseSelector": "...", "fields": [...]}`, Pattern D
  applies it directly to the HTML it just fetched — no LLM call, no
  E1 escalation. Built on selectolax (a core dep), works regardless
  of which extras are installed. Detected via the new
  `_extractors.css.looks_like_css_schema` helper. Pydantic-shaped
  schemas continue to route through E1's LLM path unchanged.
- **`intermediate_raw_text`** on every `/scrape` and `auto_scrape`
  response. Always populated when Pattern D ran, regardless of whether
  the cascade returned at D or escalated. Lets adapters recover D's
  HTML for custom in-process parsers (Megazip's selectolax fallback)
  without depending on the sidecar's success classifier.
- **`escalation_log: list[dict]`** on responses. Each entry carries
  `{step, outcome, reason, duration_s, detail}`. Replaces the opaque
  `pattern_attempts: list[str]` for ops debugging — operators can now
  see WHY a cascade escalated, not just WHICH steps ran. Outcomes:
  `won | failed | rejected | skipped`. Reasons: `ok | blocked |
  no_signal | extra_missing | exception`. The legacy `pattern_attempts`
  field stays alongside (derived).
- **Smart defaults — auto-CF detection** (`solve_cloudflare="auto"`
  default). Pattern D's first fetch goes WITHOUT the solver. If the
  response body matches a CF challenge fingerprint
  (`<title>Just a moment...</title>` / `cf-mitigated"` / status 403/503
  with CF body), redo with the solver. Saves ~10s per call on vendors
  that don't gate behind CF. Disable with explicit `solve_cloudflare:
  true` or `false`.
- **Smart defaults — auto-SPA detection**. After Pattern D returns no
  signal, if the HTML looks like an unhydrated SPA shell (small + has
  `id="root"`, `data-reactroot`, `window.__NUXT__`, etc.), retry once
  with `network_idle=true`. Saves operators from having to know which
  vendors need the SPA-hydration wait.
- **New `_extractors/` module** — extractor registry with built-ins:
  `json_ld_product` (lifted from existing B/C), `microdata_price`
  (lifted from existing C), `css` (NEW; selectolax), `open_graph`
  (NEW; OpenGraph product tags). All exposed via
  `scrapper_tool._extractors.get(name).extract(html)`.
- **`solve_cloudflare`** field on `ScrapeRequest`. Accepts
  `"auto"` (default), `True`, or `False`.
- **`/metrics` endpoint** — Prometheus exposition.
  `scrapper_pattern_used_total`, `scrapper_responses_structured_total`,
  `scrapper_responses_unstructured_total`,
  `scrapper_pattern_duration_seconds` (histogram per step+outcome),
  `scrapper_cascade_steps`, `scrapper_user_data_dir_reused_total`.
  Returns 503 when `prometheus-client` isn't installed (older `[http]`
  installs); the bundled Docker image always has it. Adds
  `prometheus-client>=0.20` to the `[http]` extra.

### Changed

- **Pattern D's default behavior changed**: `solve_cloudflare` is now
  `"auto"` (was implicit `True` in v1.3.0). Most callers see lower
  cold-call latency for non-CF vendors; CF-protected vendors still
  get the solver via auto-detection. To restore strict v1.3.0 behavior,
  set `solve_cloudflare: true` explicitly.

### Documentation

- **New `docs/cookbook.md`** with worked recipes per vendor shape:
  server-rendered LD+JSON (Amayama), SPA with CSS cards (Tasca),
  D-then-custom-parser hybrid (Megazip), LLM extraction with strict
  schema, pure D no-escalation, cross-request CF clearance reuse,
  force-pattern shortcuts. Plus debugging recipes (`escalation_log`
  walkthrough, `/ready` capability check, `/metrics` query).
- `docs/http-sidecar.md` — new "Two paths: simple by default,
  controllable when you need it" section near the top. Smart-defaults
  table. Response-anatomy breakdown. `/metrics` documentation.

### Migration notes

- All additions are additive. No callers break.
- Adapters serving SPA vendors should pass a CSS-shaped `schema_json`.
  See `docs/cookbook.md` Recipe 2 (Tasca) and Recipe 3 (Megazip).
- Adapters that pin `solve_cloudflare: true` keep working but waste
  ~10s on non-CF probes. Drop the flag to opt into auto-detection.
- The legacy `pattern_attempts` is still emitted; switch consumers to
  `escalation_log` opportunistically. Targeted for v2.0.0 removal.

## [1.3.0] - 2026-05-06

Tasca-cost release. Pattern D's Cloudflare clearance now carries forward to
E1/E2 within a cascade invocation via a shared per-request browser profile,
eliminating the redundant CF challenges that pre-1.3.0 caused E-tier
escalations to fresh-fight already-bypassed sites.

### Estimated impact (Tasca shape — D fetches but extraction needs LLM)

- **Pre-v1.3.0**: D solves CF (6-15s), discards. E1 fresh-fights CF (LLM call
  wasted, ~30s, often fails). E2 fresh-fights CF (LLM call wasted, ~30s).
  Total ~70s, 2 LLM calls, no result.
- **Post-v1.3.0**: D solves CF (6-15s), captures hydrated HTML. If still no
  signal → E1 inherits the cleared profile, navigates straight to result
  rows, LLM extracts in 5-10s. Total ~25s, 1 LLM call, result returned.

Net: **~64% latency cut, 50% LLM cost cut, plus failure → success conversion**
for the entire class of "D defeats CF but the page schema needs an LLM."

### Added

- **Per-cascade ephemeral browser profile** (default behavior). When
  `mode=auto` or `mode=hostile` runs against a request that might invoke
  Pattern D, the sidecar creates a `tempfile.mkdtemp("scrapper-cascade-")`
  and threads it as `user_data_dir` to D, E1, and E2. Cookies (including
  `cf_clearance`) persist on disk between launches, so once D solves CF,
  E1/E2 inherit the cleared session. Dir is cleaned up on every exit path
  (success, blocked, exception).
- **`persist_browser_profile_dir: str | None = None`** on `ScrapeRequest`.
  Caller-provided absolute path that overrides the ephemeral default. Use
  for poll-style workloads that hit the same domain repeatedly. Caller
  owns lifecycle: per-vendor isolation, ~30 min TTL rotation to dodge
  Cloudflare's stale-profile detection. The sidecar NEVER deletes a
  caller-provided dir.
- **`AgentConfig.user_data_dir`** field. Threaded through to
  `crawl4ai.BrowserConfig` (with `use_persistent_context=True`) and
  `browser_use.BrowserConfig` when set. Env var
  `SCRAPPER_TOOL_AGENT_USER_DATA_DIR` for direct callers of
  `agent_extract` / `agent_browse` who want cross-call persistence
  outside the cascade.
- **`/ready.checks.user_data_dir_supported`** capability probe + warning.
  Inspects the installed Crawl4AI / browser-use `BrowserConfig`
  signatures (no browser launch — pure introspection). When either lib
  is missing the kwarg (older releases), the warning surfaces so
  operators know D's CF clearance won't carry forward to E1/E2.
- **`hostile_only` + `hostile_fallback` + `pattern_d_network_idle` +
  `persist_browser_profile_dir`** on the MCP `auto_scrape` tool. Mirrors
  the REST request fields. The MCP tool also manages its own profile-dir
  lifecycle (mkdtemp on entry, rmtree in finally).

### Internal refactors

- Extracted `_resolve_profile_dir` helper. Encapsulates the (mode, extra,
  caller-dir) decision matrix that picks ephemeral vs persistent vs none.
- Extracted `_do_scrape_inner` / `_auto_scrape_inner` from the wrapping
  try/finally. No behavior change; just keeps the cascade body legible.

### Migration notes

- Default behavior change: `mode=auto` and `mode=hostile` now create a
  temp dir on every invocation that might invoke D (when `[hostile]` is
  installed). Disk I/O overhead: ~1-3 MB per request, ~1-3s of mkdtemp
  + cleanup. On high-throughput sidecars (>10 req/s), monitor inode
  pressure. Cleanup is in `try/finally` so dirs don't accumulate on
  failure paths.
- Operators wanting the pre-v1.3.0 behavior (no shared dir) can keep
  `[hostile]` uninstalled — the cascade then falls through to E1
  directly, just like v1.1.x.
- Adapters wanting cross-request reuse: set
  `persist_browser_profile_dir=/var/lib/scrapper/profiles/<vendor>/`.
  Per-vendor isolation + ~30 min rotation are caller responsibilities.
- If `/ready.checks.user_data_dir_supported=false`, your installed
  Crawl4AI / browser-use versions silently ignore `user_data_dir`. The
  cascade still works, but D's CF clearance doesn't carry forward.
  Upgrade: `pip install -U crawl4ai>=0.6 browser-use>=0.5`.

## [1.2.0] - 2026-05-06

Tasca-unblock release. Three additive request fields and one additive response
field that let SPA-rendered hostile vendors work end-to-end and let downstream
consumers stop reimplementing the success classifier. Builds on v1.1.3's
Pattern D wiring.

### Added

- **`mode="hostile"`** on `ScrapeRequest`. Invokes Pattern D directly, skipping
  the 4-profile A/B/C ladder. Use for vendors recon-classified as hostile
  (Cloudflare Turnstile, Akamai EVA, DataDome) where A/B/C is known to fail.
  Saves ~2-3s per call by skipping the doomed profile attempts and cuts 4
  noise entries from `pattern_attempts`. Pairs with the new
  `hostile_fallback: bool = True` field — set False to surface D failures
  rather than silently paying for an LLM call (returns 422 on D-fetch
  failure, 503 when `[hostile]` extra is missing).
- **`pattern_d_network_idle: bool = False`** on `ScrapeRequest`. When True,
  Pattern D's Scrapling fetcher waits for the page's network to settle before
  returning HTML. Required for SPA-rendered hostile vendors (Tasca,
  RevolutionParts dealers behind CF) where results lazy-load via JS after CF
  clearance. Adds ~5-15s to D's fetch time; auto-bumps the per-fetch timeout
  floor to 30s when set.
- **`hostile_only: bool = False`** + **`hostile_fallback: bool = True`** +
  **`pattern_d_network_idle: bool = False`** on the MCP `auto_scrape` tool.
  Same semantics as the REST request fields.
- **`is_structured: bool`** on every `/scrape` and `auto_scrape` response.
  True when the sidecar's classifier accepted the page (A/B/C / D succeeded
  with structured signal, OR E1/E2 returned `data` without the `_raw`
  free-form-text marker). False on blocked / errored / `_raw`-only responses.
  Removes the need for downstream consumers to derive this from response
  shape; the sidecar already classifies via `_classify_extraction_success`
  (A/B/C, D) and `_is_e_tier_structured` (E1, E2). Replace local derivation
  with `payload.get("is_structured", False)`.

### Internal refactors

- Extracted `_do_scrape_e_tier` helper from `_do_scrape` to share the E1 → E2
  cascade between `mode="auto"` and `mode="hostile"` (with `hostile_fallback=True`).
  No behavior change for the existing path; the helper is a pure lift.
- Extracted `_continue_to_e_tier` helper from MCP `auto_scrape` for the same
  reason — shared between the normal cascade and the new `hostile_only=True`
  path.

### Migration notes

- All four fields are additive; no existing callers break.
- Downstream consumers reading the response can replace local `is_structured`
  derivation with `payload.get("is_structured", False)`. The three-iteration
  shape-guessing pattern (PartsPilot's `agent_client.ScrapeResult.is_structured`)
  becomes a 1-line read.
- For MCP clients that need `mode="hostile"` semantics, use
  `auto_scrape(url, hostile_only=True)`.
- For SPA-rendered hostile vendors, set `pattern_d_network_idle=True` (REST)
  or pass it to `auto_scrape` (MCP).

## [1.1.3] - 2026-05-05

Cascade-correctness fix. The `/scrape` endpoint and `auto_scrape` MCP tool documented their auto-escalation as **A/B/C → D → E1 → E2** since v1.1.0, but the implementation actually ran **A/B/C → E1 → E2** — Pattern D (Scrapling, the cheap-but-stealthy escalation for Cloudflare-Turnstile / Akamai EVA / DataDome vendors) was unreachable from either surface even when the `[hostile]` extra was installed. Reported by a downstream API consumer 2026-05-04. Result: every hostile vendor that A/B/C couldn't read paid for an LLM call (E1 / E2) that Pattern D could have served for free. The bundled Docker image (`ghcr.io/valerok/scrapper-tool:latest`) ships `[hostile]` via `[full]`, so most published-image users will see immediate cost relief on hostile vendors after upgrading.

### Fixed

- **`/scrape` (REST) and `auto_scrape` (MCP) now invoke Pattern D between A/B/C and E1.** When A/B/C raises `BlockedError` (or, with `schema_json` set, returns no structured signal), the cascade tries `scrapper_tool.patterns.d.hostile_client` — Scrapling's `StealthyFetcher` with `solve_cloudflare=True` — before paying for an LLM call. Pattern B/C extraction runs over D's HTML using the same success classifier as the A/B/C step, so D is "A/B/C with a stronger fetcher" semantically. On success, the response carries `pattern_used="d"`. Pre-1.1.3 these surfaces never invoked Pattern D regardless of `[hostile]` install state.
- Pattern D is invoked only when the `[hostile]` extra is installed (probe via `_hostile_available`). When it isn't, the D step is skipped silently — `pattern_attempts` does NOT include `"d"` — and the response carries `hostile_skipped: true` so operators can see at a glance that an LLM call was paid where Scrapling could have served the page.
- D failures (Scrapling itself blocked, network error, page returned no signal) fall through to E1 with `"d"` recorded in `pattern_attempts` for traceability.
- The v1.1.2 `force_llm_extract=true` flag still routes to the LLM as before — D inherits the same opt-out, so `force_llm_extract` callers still reach E1 even when D could have read the page. The flag's contract ("I want the LLM to apply my schema") is preserved.

### Added

- **`hostile_skipped: bool`** on every `/scrape` and `auto_scrape` response. `true` means the cascade reached the D step but `[hostile]` wasn't installed; `false` means D was either invoked (successfully or not) or never reached (e.g. A/B/C succeeded). Use this in observability to gauge `pip install scrapper-tool[hostile]` install ROI for your traffic.
- **`/ready.checks.warnings: list[str]`**. Currently emits `"hostile_not_installed: cascade will skip Pattern D and pay LLM costs on hostile vendors. Install with: pip install scrapper-tool[hostile]"` when `[hostile]` is missing. Does NOT change `status` — operators that want a hard gate can grep `warnings` themselves; the existing `ready` / `degraded` / `not_ready` boundary is preserved.
- **`pattern_used` enum gains `"d"`.** New full set: `"a_b_c" | "d" | "e1" | "e2"`. Strict additive change — callers that branch on the existing three values keep working with a new branch they can opt to handle.
- New unit-test classes covering the D path: `TestScrapeWithPatternD` (7 cases — D wins, D missing, D fails → E1, `force_llm_extract` short-circuit, `mode="fetch"` does not invoke D, `/ready` warnings emitted/suppressed) and `TestAutoScrapeWithPatternD` (D win + D-skipped-no-extra) on the MCP surface.

### Migration notes

- **Docker users**: `docker compose pull && docker compose up -d --force-recreate`. The `1.1.3` image still ships `[hostile]` via `[full]`, so the cascade gains Pattern D on the first restart with no compose changes.
- **`pip install scrapper-tool[http]` users**: install layer unchanged; you keep the lean default. To get Pattern D, add `[hostile]` (`pip install scrapper-tool[http,hostile]`) — note the `[hostile]` / `[llm-agent]` `lxml` conflict in `[tool.uv].conflicts`, install via `[full]` if you also need Pattern E.
- **Callers reading `pattern_used`**: handle `"d"` as a success case alongside `"a_b_c"`. The response shape (top-level `product`, `json_ld`, `microdata_price`, `raw_text`) matches the A/B/C path because D runs the same B/C extractors.
- **No breaking changes** to the REST or MCP request schemas. `force_llm_extract`, `mode`, `schema_json`, etc. all behave identically.

## [1.1.2] - 2026-05-03

Image hardening + behaviour-correctness release. Six gaps surfaced by a downstream consumer ([PartsPilot affiliate-service](https://github.com/ValeroK/affiliate-service) 2026-05-02 manual smoke against the published `1.1.0` image) — four packaging gaps that made Pattern E silently broken, and two behaviour gaps that wasted LLM budget. All fixed upstream so consumers no longer carry runtime-install workarounds in their compose stacks.

### Breaking changes

- **Default image `ENTRYPOINT`** flipped from `scrapper-tool-mcp` (stdio MCP) to `scrapper-tool-serve` (REST sidecar on port 5792). The README + `docs/http-sidecar.md` already treat the REST sidecar as the primary surface for non-MCP callers; the entrypoint now matches. **MCP-mode users override** with `entrypoint: ["scrapper-tool-mcp"]` in compose — see `docs/mcp.md`. The MCP entrypoint is unchanged; only the *default* moved.
- The image's `HEALTHCHECK` now probes `GET /health` over the REST sidecar (port 5792) instead of importing Python modules. This matches the new entrypoint; MCP-mode users either ignore the healthcheck or override with `--health-cmd`.

### Fixed (image — were silent breakage in v1.1.0/1.1.1)

- **Bundle Playwright Firefox by default**. The runtime stage now runs `playwright install firefox` alongside Chromium. Pattern E1 (`agent_extract` via Crawl4AI) and Pattern E2 (`agent_browse` via browser-use) need Firefox; previously the pip extras were installed but the binary was missing, so any Pattern E call against a real site 500'd with `BrowserType.launch: Executable doesn't exist at firefox-*/firefox`. `/ready` lied about this — see the `agent_runnable` fix below.
- **Add the GTK / Cairo / GDK / X11 libs Firefox needs to launch**. The runtime stage adds `libgtk-3-0`, `libpangocairo-1.0-0`, `libgdk-pixbuf-2.0-0`, `libcairo-gobject2`, and `libxcursor1`. Without these, Firefox crashes at launch with `Host system is missing dependencies to run browsers`.
- **Pin `camoufox[geoip]>=0.4`** in the `[llm-agent]` extra (was bare `camoufox>=0.4`). Camoufox v0.4+ raises `NotInstalledGeoIPExtra` on every browser launch unless `geoip2` is installed; the extra was effectively non-functional out of the box.
- **`INSTALL_CAMOUFOX` build arg now defaults to `1`** in the bundled Dockerfile. `camoufox fetch` runs at build time so the stealth Firefox profile is on disk for callers that flip `SCRAPPER_TOOL_AGENT_BROWSER=camoufox`. Override with `--build-arg INSTALL_CAMOUFOX=0` for a smaller image.

### Fixed (behaviour)

- **`/ready` reports `agent_runnable` separately from `agent_installed`** (was: only the latter). `agent_installed=true` only proves the Python `[llm-agent]` extra is importable; `agent_runnable=true` additionally proves the on-disk browser binary for the configured `SCRAPPER_TOOL_AGENT_BROWSER` is present. **Operators should gate Pattern E calls on `agent_runnable`, not `agent_installed`.** Status resolution: `ready` requires `agent_runnable && llm_reachable && llm_model_available`; otherwise `degraded` (sidecar can still serve A/B/C cheaply via `/fetch` and `/scrape mode=fetch`); `not_ready` only when the extra itself is missing.
- **`/scrape mode=auto` no longer always escalates to E1 when `schema_json` is set.** The pre-1.1.2 success heuristic conflated "page blocked" with "page readable but no JSON-LD" — both forced an LLM call. From 1.1.2, a `mode=auto` request with `schema_json` accepts A/B/C as success when the page returned 2xx and any structured signal (`json_ld`, `microdata_price`, or auto-detected `product`) was extracted. Callers that prefer the old always-escalate behaviour set `force_llm_extract: true` on the request body.

### Added

- `force_llm_extract: bool = False` field on `ScrapeRequest`. Reverts the v1.1.2 escalation behaviour change for callers that genuinely need the LLM to apply their custom schema even when A/B/C returned readable content.
- `tests/integration/test_image_smoke.sh` — Docker image smoke that boots the freshly-built image and asserts `/health`, `/version`, `/ready` (with `agent_runnable=true`), and a `POST /scrape mode=fetch` against `https://example.com`. Wired into `docker-release.yml` between the local build step and the publish step — the release tag is **never pushed if the smoke is red**. Catches the entire class of "image declared a capability that doesn't actually work" gaps that drove this release.

### Migration notes

- **Most consumers**: `docker compose pull scrapper-tool && docker compose up -d --force-recreate scrapper-tool` is enough. The default REST entrypoint is what the README documented all along.
- **MCP-mode users**: add `entrypoint: ["scrapper-tool-mcp"]` to your compose service, OR run with `--entrypoint scrapper-tool-mcp` on `docker run`. The `scrapper-tool-mcp` console script is unchanged.
- **Callers relying on `agent_installed` for capability checks**: switch to `checks.agent_runnable` in the `/ready` response. The old field is still emitted for backward compat but no longer governs the `status` field.
- **Callers that depended on `mode=auto` always reaching E1**: set `force_llm_extract: true` on the request body. Most callers don't and will see lower latency + lower LLM cost as a free upgrade.

## [1.1.1] - 2026-05-02

Security-only patch release. Resolves three Dependabot alerts on the `litellm` transitive dependency: SQL injection in proxy API key verification (critical, GHSA), SSTI in `/prompts/test`, and authenticated RCE via MCP stdio test endpoints. All three were fixed upstream in `litellm 1.83.7`.

### Removed

- `[skyvern-backend]` install extra. It was the only consumer of the vulnerable `litellm` versions, was already declared conflicting with `[full]` in `[tool.uv].conflicts`, and was never bundled in any published Docker image or recommended SDK install path. `skyvern>=1.0.32` itself has internally inconsistent transitive pins (requires both `litellm>=1.83.7` and `jsonschema>=4.25.1`, but `litellm>=1.83.7` pins `jsonschema==4.23.0`), so the extra cannot be re-locked at a safe version until upstream skyvern fixes its dep tree. No consumer impact — this was an opt-in alternative agent backend with no published install path that pulled it.

### Migration notes

- If you were using `pip install 'scrapper-tool[skyvern-backend]'` (no known consumers), pin `scrapper-tool<1.1.1` and continue using v1.1.0, or wait for an upstream skyvern release that fixes the transitive dep conflict.
- All other install paths (`scrapper-tool[full,agent,http]`, `[hostile]`, `[llm-agent]`, `[agent]`, `[http]`) are unaffected and behave identically to v1.1.0.

## [1.1.0] - 2026-05-02

Adds an **HTTP REST sidecar** so any service (not just MCP-aware LLM agents) can call scrapper-tool over plain JSON. The new `/scrape` endpoint runs the full A/B/C → E1 → E2 escalation ladder server-side, removing per-pattern decision logic from callers. The MCP server gains a matching `auto_scrape` tool and a structured-extraction shortcut on `fetch_with_ladder`. New `[http]` install extra and `scrapper-tool-serve` console script.

### Added

- M15 — **HTTP REST sidecar** (`scrapper_tool.http_server` + `scrapper-tool-serve` console script). FastAPI 3.1 server on port **5792** with seven endpoints:
  - `POST /scrape` — primary auto-escalating endpoint (A/B/C → E1 → E2). Returns `pattern_used` + `pattern_attempts` so callers can see what worked.
  - `POST /fetch` — Pattern A/B/C with optional Pattern B/C structured extraction (`extract_structured: true` adds `product`, `json_ld`, `microdata_price`).
  - `POST /extract` — Pattern E1 direct.
  - `POST /browse` — Pattern E2 direct.
  - `GET /health` — liveness.
  - `GET /ready` — readiness with detailed component checks (Ollama reachability, model availability, browser binary detection).
  - `GET /version` — version + installed-extras info.
  - Plus FastAPI-generated `/docs` (Swagger UI), `/redoc`, and `/openapi.json` (toggle off with `SCRAPPER_TOOL_HTTP_DOCS=0`).
- M15 — Optional `X-API-Key` auth (`SCRAPPER_TOOL_HTTP_API_KEY`) on POST endpoints; operational endpoints + docs always unauthenticated. Configurable CORS via `SCRAPPER_TOOL_HTTP_CORS_ORIGINS`.
- M15 — `[http]` install extra (`pip install scrapper-tool[http]`) — pulls FastAPI + uvicorn + PyYAML.
- M15 — `docker-compose.yml` `rest` profile: `docker compose --profile rest up -d scrapper-tool-rest` exposes the sidecar on port 5792.
- M15 — MCP `fetch_with_ladder` tool gains `extract_structured: bool = False` parameter — runs Pattern B + C in one tool call. Eliminates the common two-tool fetch+extract pattern for LLM agents.
- M15 — New MCP tool `auto_scrape(url, schema_json?, instruction?, model?, browser?, timeout_s?)` — same auto-escalation ladder as the HTTP `/scrape` endpoint. Recommended first tool for new MCP integrations.
- M15 — `scrapper_tool.errors.ConfigurationError(ScrapingError)` — raised when a required component is missing or misconfigured locally (browser binary not found, extra not installed, model not pulled). Maps to HTTP 503 with `{"error": "configuration_error"}`. Distinct from `AgentLLMError` (live connectivity).
- M15 — Auto-generated, committed OpenAPI 3.1 spec at [`docs/openapi/openapi.json`](docs/openapi/openapi.json) and [`docs/openapi/openapi.yaml`](docs/openapi/openapi.yaml). Generated by `scripts/dump_openapi.py`. CI guards against drift via the new `openapi-spec-check` job. Enables typed-client codegen for the affiliate service (`openapi-python-client` / `openapi-typescript-codegen`).
- M15 — Documentation:
  - [`docs/http-sidecar.md`](docs/http-sidecar.md) — full reference with quick start (3 commands), endpoint table, error codes, affiliate-service wiring example, and a dedicated "LLM reference" section with cross-references.
  - [`docs/agent-integration.md`](docs/agent-integration.md) — updated tool table for `auto_scrape` and the new `fetch_with_ladder` parameter.
  - [`docs/SETTINGS.md`](docs/SETTINGS.md) — new "HTTP REST sidecar" section with all `SCRAPPER_TOOL_HTTP_*` env vars.
  - [`docs/quickstart.md`](docs/quickstart.md) — populated from stub. Now covers Patterns A/B/C/E with copy-paste examples.
  - [`docs/index.md`](docs/index.md) — new HTTP sidecar entry in the table of contents.
  - [`.env.example`](.env.example) — new HTTP server section.
- M15 — CI: new `openapi-spec-check` job verifies the committed spec stays in sync with the code; test matrix entry `dev,full,agent,http` exercises the full HTTP server path.

### Changed

- `AgentConfig.max_steps` default raised from `20` to `50` to handle deeper E2 workflows out of the box. Override via `SCRAPPER_TOOL_AGENT_MAX_STEPS` if you want the old behaviour.
- MCP `agent_browse` tool default `max_steps` raised from `30` to `50` for parity with `AgentConfig`.
- MCP `FastMCP(instructions=...)` updated to mention `auto_scrape` as the recommended first tool.

### Migration notes

- **No breaking changes.** The new `extract_structured` parameter on the MCP `fetch_with_ladder` tool defaults to `False`, preserving existing callers' return shapes.
- If you currently call multiple endpoints by hand to escalate (e.g. `fetch_with_ladder` then `agent_extract` on 422), switch to the unified `/scrape` HTTP endpoint or `auto_scrape` MCP tool for a simpler integration.
- The bumped `max_steps` default is a behavioural change but only affects E2 calls that previously hit the 20-step ceiling — those return more data now instead of `error="no-match"`.

## [1.0.0] - 2026-05-02

First stable release. Adds **Pattern E** — local-LLM-driven scraping for any protected site — and ships a unified Docker image / SDK install (`[full]`) that bundles every pattern in one environment. Zero API cost: the LLM runs externally (Ollama / LM Studio / llama.cpp / vLLM) and the lib calls out to it.

The public API (`scrapper_tool.{request_with_ladder, hostile_client, agent_extract, agent_browse, agent_session, AgentResult, AgentConfig, errors.*}`) and the MCP tool surface (`fetch_with_ladder`, `extract_product`, `extract_microdata_price`, `canary`, `agent_extract`, `agent_browse`) are now considered stable under SemVer.

### Added

- M14 — `scrapper_tool.agent` package + `patterns/e.py` re-export shim. Two modes:
  - `agent_extract(url, schema, …)` — E1: render with stealth browser + 1 LLM call to produce structured JSON. Default for "scrape any data". Backed by Crawl4AI.
  - `agent_browse(url, instruction, …)` — E2: multi-step LLM-driven agent loop for interactive tasks (login, paginate, dynamic forms). Backed by browser-use.
  - `agent_session()` async context manager for batched calls with a warm config.
- M14 — Five-backend stealth browser stack (`scrapper_tool.agent.backends.browser`): **Camoufox** (default — ~0% headless detection on 2026 benchmarks), Patchright (fast mode), Zendriver, Botasaurus, Scrapling.
- M14 — Multi-backend LLM (`scrapper_tool.agent.backends.llm`): Ollama (default), llama.cpp / vLLM / generic OpenAI-compat. Pre-flight `/api/tags` probe at session start so misconfiguration fails fast.
- M14 — Two-tier free OSS captcha cascade (`scrapper_tool.agent.backends.captcha`): Tier 0 Camoufox auto-pass → Tier 1 [Theyka/Turnstile-Solver](https://github.com/Theyka/Turnstile-Solver). Optional paid Tier 2 (CapSolver, NopeCHA, 2Captcha) auto-engages when `SCRAPPER_TOOL_CAPTCHA_KEY` is set. Disclaimer in docs: no OSS solver matches CapSolver's coverage of hCaptcha v3 / reCAPTCHA v3 / Funcaptcha / DataDome.
- M14 — Humanlike behavior layer (`scrapper_tool.agent.backends.behavior`): jittered keystrokes (log-normal 60–180 ms median), bezier mouse paths, variable scroll cadence. Defeats DataDome behavioral detection. `fast` / `off` policies for unprotected sites.
- M14 — Per-session Browserforge fingerprint generator (`scrapper_tool.agent.backends.fingerprint`) for non-Camoufox backends.
- M14 — MCP server gains two new tools: `agent_extract` and `agent_browse`. Same lazy-import + helpful "extra not installed" error envelope as the `[hostile]`/`[agent]` flow. Body, screenshot, and DOM-snippet truncation to bound MCP context.
- M14 — Agent error hierarchy: `AgentError`, `AgentTimeoutError`, `AgentBlockedError` (multi-inherits `BlockedError` for backward compat), `AgentLLMError`, `AgentSchemaError`, `CaptchaSolveError`.
- M14 — `[full]` extra: bundles `[hostile]` + `[llm-agent]` + `[turnstile-solver]` so all five patterns coexist. Uses `[tool.uv] override-dependencies = ["lxml>=6.0.3"]` to coerce Scrapling (`lxml>=6`) and Crawl4AI (`lxml~=5.3`) onto a single resolved lxml. uv-only.
- M14 — Single Docker image (`Dockerfile`) bundling Pattern A/B/C/D/E + MCP server. Built on `[full,agent]`. Docker-compose ships the image talking to an external LLM via `host.docker.internal` (Ollama / LM Studio / llama.cpp / vLLM). Image does NOT bundle an LLM. `INSTALL_CAMOUFOX=1` build arg bakes in Camoufox's patched Firefox (+300 MB).
- M14 — `docker-release.yml` workflow publishes the image to GHCR (`ghcr.io/valerok/scrapper-tool`) on every `v*.*.*` tag with `<version>`, `<major>.<minor>`, and `latest` tags.
- M14 — `scripts/e2e/bench_model.py` + `scripts/e2e/compare_benches.py` — benchmark harness that runs E1/E2 trials against the loaded LM Studio model and produces a side-by-side markdown comparison report. Used in CI hand-off to validate model swaps.
- M14 — `scripts/e2e/test_mcp_session.py` + `test_mcp_session_http.py` — sample MCP-client SDK scripts that exercise every advertised tool over stdio (in-container) and streamable-HTTP transports.
- M14 — `live-agent.yml` GitHub workflow — manual-dispatch live canary running `tests/integration/test_agent_live.py` against the image, auto-opens issue on regression.
- M14 — Documentation:
  - [`docs/patterns/e-llm-agent.md`](docs/patterns/e-llm-agent.md) — when to use which mode, hardware sizing, captcha disclaimer, ToS notes.
  - [`docs/SETTINGS.md`](docs/SETTINGS.md) — full env-var reference, where settings go when used as a library, install-extras matrix.
  - [`.env.example`](.env.example) — drop-in starter file with every variable annotated.
  - README extended with capability matrix, MCP runtime + Docker run guide, external-LLM (LM Studio / llama.cpp / vLLM / remote Ollama) wiring table.
- M14 — Tests: 21 new test files (`tests/unit/test_agent_*.py`, `tests/integration/test_agent_live.py`), browser-use / Crawl4AI all mocked so unit tests run in the default `[dev]` install. **247 tests pass**, 86.7% coverage.

### Fixed
- M14 — `agent_extract` for object schemas with a single top-level array property: smaller LLMs (e.g. qwen3-vl-8b) sometimes return the flat list `[item, item, …]` directly instead of wrapping it in `{key: [items]}`. `_unwrap_crawl4ai_singleton` now detects this shape and re-wraps to match the schema. Discovered via the new bench harness comparing gemma-4-e4b vs qwen3-vl-8b.
- M14 — Switched `browser_use` LLM bridge from langchain wrappers to `browser_use.llm.{ollama,openai}.chat.*` native classes; browser-use 0.5+ rejects langchain chat objects with `ValueError: invalid llm`.

### Changed
- Bumped to `v1.0.0` (graduating from alpha — public API and MCP tool surface are now stable under SemVer). Default `AgentConfig` model is `qwen3-vl:8b` (May 2026 SOTA for agentic UI grounding at 16 GB VRAM).
- `[hostile]` extra now requests `scrapling[fetchers]>=0.3` (was `scrapling>=0.3`) so `StealthyFetcher` actually loads — Scrapling 0.4 split browser deps into a `[fetchers]` extra. This was a latent runtime-only failure before.
- CI matrix extended to four extras combinations: `[dev,agent]`, `[dev,hostile,agent]`, `[dev,llm-agent,agent]`, `[dev,full,agent]`.

### Removed
- Bundled Ollama service from `docker-compose.yml`. The container always calls an external LLM endpoint via `host.docker.internal` (or a remote URL) — a deliberate simplification.

## [0.2.0] - 2026-05-01

Adds an optional MCP server so LLM agents (Claude, OpenClaw, Hermes Agent, AutoGen, LangChain) can drive the scraper directly. Last milestone of the M0–M13 roadmap.

### Added
- M13 — MCP server (`scrapper_tool.mcp`) + `scrapper-tool-mcp` console script. Lazy-imports the `mcp` SDK; missing-extra error surfaces a useful install hint. Four tools exposed:
  - `fetch_with_ladder(url, method?, use_curl_cffi?)` — issues an HTTP request through the four-profile impersonation ladder; returns `{status, body (≤64 KB), winning_profile, blocked, error}`.
  - `extract_product(html, base_url?)` — parses schema.org Product+Offer via extruct; returns ProductOffer dict or null.
  - `extract_microdata_price(html)` — parses `<meta itemprop="price">` + `priceCurrency`; returns `{price, currency}` or null.
  - `canary(url, profiles?)` — walks the impersonation ladder; returns the same JSON shape as the CLI's `--json` mode.
- M13 — `[agent]` optional extra now pulls `mcp>=1.2`. Empty-extra placeholder (M0/M8.5) replaced.
- M13 — Body truncation at 64 KB so a single fetch can't blow an agent's context window. `truncated: true` flag signals when this happened.
- M13 — `docs/agent-integration.md` — comprehensive integration guide covering Claude Desktop / Claude Code / Anthropic SDK + mcp-use / OpenClaw / Hermes / AutoGen / LangChain. Security section codifies trust-boundary handling (the consuming agent's permission model gates user-data-bearing fetches; the lib doesn't bundle auth).
- M13 — 15 MCP tests (4 tool dispatch tests with FakeCurlSession-mocked HTTP, 2 truncation tests, 3 main-entrypoint tests, lazy-import safety, +6 edge cases). Coverage on `mcp.py` 86%.
- M11.5 — Live-canary GitHub Actions workflow (`.github/workflows/live-canary.yml`). Daily cron at 04:17 UTC + `workflow_dispatch`. Three jobs (smoke / Pattern A ladder / Pattern B extraction) probe stable public URLs (`example.com`, `httpbin.org/anything`, `schema.org/Product`); on failure, a fourth job opens (or comments on) a `live-canary-failed` GitHub issue with dedup so we don't get one issue per failed run.
- M11.5 — `tests/integration/test_live_probes.py` — three opt-in tests gated by `@pytest.mark.live` + `SCRAPPER_TOOL_LIVE=1` env var. Default `pytest` invocation skips them; the live-canary workflow runs them with the env var set. CI matrix unaffected.
- M11.5 — `tests/canary_targets.yaml` — append-only, dated registry of canary URLs. Discipline: never edit historical URLs in place; add a new row above and leave the predecessor as audit trail.
- M12 — Quarterly review checklist in `CONTRIBUTING.md` — four numbered actions (re-run canary, audit `docs/research/`, audit `do-not-adopt.md`, bump MCP SDK pin) the maintainer runs each quarter to keep the lib aligned with the moving scraping landscape.

## [0.1.0] - 2026-04-30

First public release. Covers Pattern A/B/C/D extraction primitives, the four-profile anti-bot impersonation ladder, deterministic fixture-replay testing, the generic `Adapter` Protocol, and a `scrapper-tool canary` CLI.

### Added
- M0 — repo bootstrap: `pyproject.toml`, MIT `LICENSE`, README, governance files (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`), CI workflow (`.github/workflows/ci.yml` — ruff + mypy --strict + pytest + pip-audit on py3.12/3.13/3.14 matrix), tag-triggered PyPI release workflow (`.github/workflows/release.yml`, OIDC trusted-publisher).
- `[project.optional-dependencies]` placeholders for `hostile` (Scrapling) and `agent` (MCP — populated in M13).
- M1 — HTTP core extracted from PartsPilot's `affiliate-service`: `scrapper_tool.http.vendor_client()` (httpx + curl_cffi backends, async context manager) and `scrapper_tool.http.request_with_retry()` (3 attempts, exponential backoff with ±25% jitter, retries 429/5xx/transport errors, no-retry on 4xx ≠ 429, X-Request-ID injection).
- M1 — Exception hierarchy: `ScrapingError` (base), `VendorHTTPError`, `VendorUnavailable` (alias), `BlockedError`, `ParseError`. `BlockedError` and `ParseError` deliberately do NOT inherit from `VendorHTTPError` — circuit breakers should catch one but not the others.
- M1 — Optional `structlog` integration via `scrapper_tool._logging.get_logger()`; falls back to a stdlib `logging` adapter that accepts the same `key=value` kwarg shape.
- M1 — Top-level re-exports: `scrapper_tool.{vendor_client, request_with_retry, VendorHTTPError, BlockedError, ParseError, ScrapingError, VendorUnavailable, VendorHTTPClient}`.
- M2 — Anti-bot impersonation ladder (`scrapper_tool.ladder`): `IMPERSONATE_LADDER = ("chrome133a", "chrome124", "safari18_0", "firefox135")` and `request_with_ladder(method, url, ...)` walking it top-to-bottom on 403/503. First profile to return ≠403/503 wins; all-403 raises `BlockedError` with a "escalate to Pattern D" message. Each ladder step opens a fresh `curl_cffi.AsyncSession` (one-shot per profile, sessions pinned to a single fingerprint). Logs winning profile via the structured logger (`ladder.profile_won` / `ladder.profile_blocked`).
- M2 — Re-exported at top level: `scrapper_tool.{IMPERSONATE_LADDER, request_with_ladder}`.
- M2 — 9 ladder unit tests (happy path, 403→200 fallback, 503 rotate-like-403, safari wins when chrome burns, all-403 raises, custom ladder, empty ladder ValueError, default-ladder shape, header propagation). Uses an inline `_FakeCurlSession` lifted to `scrapper_tool.testing` in M6.
- M3 — Pattern B helper (`scrapper_tool.patterns.b`): `extract_product_offer(html, base_url=None)` returns a normalised `ProductOffer` Pydantic model from any of JSON-LD / microdata / RDFa Product blocks. Handles top-level Products, Products nested inside `@graph`, multi-offer lists (takes first), price/currency nested inside `priceSpecification`, brand-as-dict-or-string, image-as-list-or-dict, all `gtin{,8,12,13,14}` variants. Powered by `extruct.extract(..., uniform=True)` so one walker covers all three syntaxes.
- M3 — `ProductOffer` model fields: `name`, `sku`, `mpn` (often the OEM in automotive use cases), `gtin`, `brand`, `description`, `image`, `price` (Decimal), `currency` (ISO 4217), `availability` (raw schema.org URI), `url`. `model_config = {"extra": "ignore"}` so vendors adding fields don't break parsing.
- M3 — 10 Pattern B unit tests (JSON-LD top-level, JSON-LD inside @graph, offers as list, priceSpecification fallback, microdata, brand-as-string, no-Product-block returns None, plain HTML returns None, base_url propagation, extra-keys ignored).
- M4 — Pattern C helper (`scrapper_tool.patterns.c`): `extract_microdata_price(html) -> tuple[Decimal, str] | None` for sites that ship `<meta itemprop="price"> + <meta itemprop="priceCurrency">` schema.org microdata anchors (preferred — stable across CSS reshuffles); `extract_via_selectors(html, *, price_selector, currency_selector=None, default_currency=None)` for last-resort bespoke CSS selectors. Backed by `selectolax` (lexbor backend; 30-40× faster than BeautifulSoup at our fetch volumes).
- M4 — Internal `_coerce_decimal` strips common currency glyphs (`$`, `€`, `£`, `₪`, `¥`) and US/UK thousands-separator commas before parsing. European decimal-comma is NOT supported by default — vendor-specific normalisation is the consumer's job.
- M4 — 21 Pattern C unit tests (microdata via `<meta>` content attribute, microdata via text fallback, price-without-currency returns None, missing microdata returns None, selector with default_currency, selector with currency_selector, selector with `data-price` attribute preferred, missing element returns None, ValueError on no-currency-source, glyph stripping for 6 currency symbols, thousands-separator stripping, unparseable input returns None).
- M5 — Pattern D helper (`scrapper_tool.patterns.d.hostile_client`): async context manager wrapping Scrapling's `StealthyFetcher` for Cloudflare Turnstile / Akamai EVA / Distil-class hostile sites. Lazy-imports `scrapling` so consumers without the `[hostile]` extra installed see a useful `ImportError` with install hint rather than `ModuleNotFoundError` at import time. Forwards `headless`, `block_resources`, `timeout`, and arbitrary `extra_kwargs` to the fetcher; supports both async (`aclose`) and sync (`close`) lifecycle on exit.
- M5 — 5 Pattern D unit tests (`ImportError` raised when `[hostile]` not installed, fetcher yielded + closed on exit, `extra_kwargs` propagate, sync-close fallback for older Scrapling versions, module docstring readable without scrapling installed). Real Scrapling integration deferred to live-probe tests (`tests/integration/test_live_probes.py`, `live` marker, opt-in).
- M6 — Test helpers (`scrapper_tool.testing`): `FakeCurlSession` (drop-in mock for `curl_cffi.AsyncSession` because `respx` doesn't intercept it), `FakeResponse` (minimal duck-typed response), `replay_fixture(path, parser)` (load fixture file from disk and feed to a parser), `assert_pydantic_snapshot(obj, path, *, write_if_missing=True)` (golden-snapshot diff for Pydantic models with first-run seeding).
- M6 — Refactored `tests/unit/test_ladder.py` to use the canonical `FakeCurlSession` (M2's inline mock removed; replaced with the import).
- M6 — 12 meta-tests in `tests/unit/test_testing_helpers.py` covering FakeResponse construction, FakeCurlSession reset/configuration/calls-tracking, replay_fixture text loading, snapshot first-run-write / pass-on-match / fail-on-drift / write_if_missing=False semantics. 100% coverage on `testing.py`.
- M5.5 — Filled `docs/research/2026-04-30-landscape.md` (~250 lines, 19 numbered sources). Eight sections: TLS-impersonation libraries, browser-stealth tools, anti-bot platforms in 2026, LLM-assisted scraping, HTML parsing libraries, structured-data extraction, what's deliberately missing from the lib, and a refresh policy that makes successor landscape docs append-only history rather than edits-in-place.

- M7 — Generic `Adapter[QueryT, ResultT]` Protocol (`scrapper_tool.adapter`). Structural typing with `runtime_checkable` so `isinstance(obj, Adapter)` works without inheritance. Required surface: `vendor_id: str` attribute + `async search(query)` + `async fetch_detail(url)`. Doc-strings codify the error-bubbling contract (VendorHTTPError → breaker trips; BlockedError → escalate to Pattern D; ParseError → don't trip breaker, parser drift bug). Re-exported as `scrapper_tool.Adapter`.
- M7 — 6 Protocol tests: complete impl satisfies isinstance, missing method fails, missing field fails, search round-trip, fetch_detail round-trip, fetch_detail returns None for missing URL.
- M8 — `scrapper-tool canary` CLI (`scrapper_tool.canary` module + `[project.scripts]` entry). Walks the impersonation ladder against a target URL, reports which profile won (or all-blocked). Designed for cron / GitHub Actions to surface "chrome133a is starting to 403" before any consumer adapter notices. Flags: `--profiles chrome133a,chrome124,...` (custom ladder), `--timeout` (per-request), `--proxy`, `--json` (machine-readable output). Exit codes: 0 success, 1 all-blocked, 2 error. Public API: `run_canary()` (programmatic) + `probe_profile()` (single-profile probe).
- M8 — 12 canary unit tests covering happy-path (first profile wins, others skipped), 403 fallback (rotates), all-blocked (exit_code=1), empty ladder ValueError, custom ladder, text mode, JSON mode parseable, --profiles override, exit codes, --help, no-subcommand argparse error, malformed --profiles flag.

### Fixed
- CI: `pip-audit --skip-editable` so the build doesn't try to look up `scrapper-tool` itself on PyPI before v0.1.0 ships.

### Notes
- Initial milestone scope and decision log live in [`docs/research/2026-04-30-landscape.md`](docs/research/2026-04-30-landscape.md) (filled in during M5.5).

### Notes
- Initial milestone scope and decision log live in [`docs/research/2026-04-30-landscape.md`](docs/research/2026-04-30-landscape.md) (filled in during M5.5).
