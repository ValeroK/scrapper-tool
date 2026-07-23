# Backend leverage roadmap — Camoufox, Obscura, and the pipeline

Forward plan for getting more out of the stealth-browser backends, plus the
architectural question of **where browsers belong in the pipeline**, and a
consolidated follow-up backlog. Builds on:
- `2026-camoufox-obscura-capabilities.md` (capability/version deep-dive)
- `2026-e2-agent-framework.md` (browser-use CDP/Firefox constraint)
- the "wire captcha + prune + obscura" effort (PR #23)

---

## 1. Prior-plan completion review

The "wire captcha / prune / obscura" plan (Stages 0–8) is **fully implemented and
shipped in PR #23**. Status per stage:

| Stage | Status |
|---|---|
| 0. Shared page-hook plumbing | DONE |
| 1. Wire captcha (E1+E2) | DONE — verified firing on a real hCaptcha (Yad2) |
| 2. Wire behavior policy | DONE |
| 3. Prune Zendriver/Botasaurus | DONE |
| 4. Add Obscura backend | DONE — + 2 bugs fixed by live testing (ws→http, `--host`) |
| 5. MCP/REST alignment | DONE |
| 6. Test hardening | DONE (416 tests, 86.5%) |
| 7. Docs + config | DONE |
| 8. E2 hybrid posture | DONE (docstrings + spike). **Escalation-gate = follow-up** (see backlog F9) |

Bonus (not in the original plan, done during validation): fixed the E2↔browser-use
stealth bug (browser-use was launching its own Chromium instead of using Camoufox).

**Everything planned was completed.** The only intentionally-deferred piece is Stage 8's
*behavioral* E1→E2 escalation gate (the docs/spike parts landed; the code gate is F9).

---

## 2. Should browsers be "at the beginning" of the pipeline?

**Short answer: No — keep the cheap HTTP tier first. But make the *stealth-browser
render* tier (Pattern D) first-class and Camoufox-powered, and reach it earlier.**

### What the Yad2 testing proved
The current ladder is `A/B/C (curl_cffi) → D (Scrapling render) → E1 (LLM) → E2 (LLM agent)`.
Against Yad2 (Radware):

| Tier | Result |
|---|---|
| A/B/C curl_cffi | HTTP 200 but body = Radware **loader page** (no data) |
| **Direct Camoufox** (nav + humanize + settle) | **Full 8.5 MB page with all listings — NO LLM** |
| E1 (Crawl4AI's plain Firefox) | Radware "Are you for real" challenge (no data) |
| E2 (Camoufox via browser-use) | hit ShieldSquare hCaptcha (CDP nav too aggressive) |

The winning path used **no LLM at all**: a single Camoufox navigation + settle returned
the fully-rendered page, which the existing B/C/CSS extractors can parse.

### The recommendation
1. **Keep curl_cffi as tier 1.** For unprotected sites it's 100–1000× cheaper than any
   browser. Browsers must NOT be the default first hop.
2. **Make Pattern D a Camoufox stealth-render tier** (not Scrapling-only). A small
   `camoufox_render(url) -> html` (nav + `humanize` + settle + `page.content()`) feeding
   the existing B/C/CSS extractors gets hard-site data **cheaply and without an LLM** —
   exactly what worked on Yad2. This is higher-value than any E-tier change: it turns
   "blocked → expensive LLM" into "blocked → cheap stealth render → deterministic parse".
3. **Detect JS-challenge interstitials in the A/B/C classifier.** curl_cffi returned
   `200` with a Radware loader; the cascade must recognise loader/challenge bodies
   ("Are you for real", "Loader page", `validate.perfdrive.com`, Radware/ShieldSquare
   markers) and escalate to the render tier instead of treating `200` as success.
4. **Obscura's place, not tier 1 either:** for *lightly*-protected JS sites,
   `obscura fetch --dump markdown` is a cheap one-shot render (no Python browser stack);
   for E2 it's the Chromium/CDP backend that gives browser-use full action support once
   the `--features stealth` build lands. Neither replaces curl_cffi at the front.

### Why this matters
It reframes the cascade as **cheap-first, then cheap-stealth-render, then LLM-only-as-
last-resort** — and it's the foundation for the "learn-once/replay" follow-up (Camoufox
render once → derive a CSS recipe → replay deterministically, no browser at all on repeat).

---

## 3. Camoufox leverage roadmap

Today `CamoufoxBackend.launch` uses 4 of ~20 knobs (`headless`, `humanize`, `geoip`,
`proxy`). Phased leverage:

**Phase C0 — correctness (small, high value)**
- **Fix the `user_data_dir` gap.** `CamoufoxBackend.launch` never receives/passes
  `user_data_dir`, so the cascade's `cf_clearance` carry-forward silently doesn't apply
  to Camoufox E2. Thread `user_data_dir` + `persistent_context=True` through.
- **Upgrade Camoufox `0.4.11 → 0.5.4`** (behind the pin + test-net); adopt
  `fingerprint_preset=True` for real (not generated) fingerprints. Validate image size
  (0.5.x wheels grew a lot on some platforms).

**Phase C1 — stealth + speed (medium)**
- **`headless="virtual"` (Xvfb) in Docker** — our image already ships `xvfb`; virtual
  display is meaningfully stealthier than pure headless and may help against the exact
  behavioral walls (Radware/ShieldSquare) that beat us. Prime candidate to make E2 pass
  Yad2 without a captcha.
- **`block_images=True`** for extraction runs (Yad2 was 8.5 MB) — big bandwidth/latency win.
- **`block_webrtc=True` when a proxy is set** — closes the WebRTC IP leak.

**Phase C2 — targeting knobs (expose via AgentConfig)**
- `os`, `screen`, `locale` (e.g. `he-IL` for Israeli sites), `webgl_config`, `addons`
  (uBlock → ad/tracker blocking = faster + fewer detection scripts), `humanize=<float>`.

---

## 4. Obscura leverage roadmap

**Phase O0 — get real stealth**
- **Build/obtain the `--features stealth` image.** The Docker Hub `:latest` is NOT the
  stealth build, so `--stealth` gives only fingerprint consistency (no TLS impersonation
  / tracker blocking) — why it failed Radware. Build `cargo build --release --features
  stealth` (or find a stealth-tagged image), then **benchmark via the `canary` CLI**
  before trusting it. Until then obscura stays experimental/light-target-only.

**Phase O1 — new cheap render paths**
- **`obscura fetch --dump markdown`** as an E1-lite path: render + clean markdown in one
  call, no Python browser stack — good for lightly-protected JS pages (feed the markdown
  straight to the LLM, or skip the LLM entirely for simple pages).
- **`obscura scrape --concurrency N`** as the parallel batch primitive (feeds the
  deferred crawl/map follow-up).

**Phase O2 — protocol-level speed + E2**
- CDP **`Fetch` interception** to block images/ads/trackers for faster renders.
- Once stealth-built, evaluate Obscura as the **preferred E2 backend** (Chromium/CDP →
  full browser-use action support, unlike Camoufox/Firefox — see the CDP constraint note).
- Tuning: `OBSCURA_SCRIPT_DEADLINE_MS` for heavy SPAs, `--v8-flags --max-old-space-size`.

---

## 5. Consolidated follow-up backlog (prioritized)

Merges the prior plan's deferrals with findings from validation + the deep-dive. Roughly
ordered by value/effort.

| # | Item | Source | Notes |
|---|---|---|---|
| F1 | **Camoufox as Pattern-D stealth render** (§2) + JS-challenge detection | new (Yad2) | Highest value: cheap no-LLM path for hard sites. Foundation for F2. |
| F2 | **Learn-once / replay-deterministic** (E1/render → CSS recipe → replay via Pattern C) | prior plan | Stops per-page LLM cost on repeat-vendor scraping. Pairs with F1. |
| F3 | **Fix Camoufox `user_data_dir` gap** (Phase C0) | deep-dive | Small; real bug — cf_clearance carry-forward broken for E2. |
| F4 | **Upgrade Camoufox 0.4.11→0.5.4** + `fingerprint_preset` (Phase C0) | deep-dive | Behind the dep-pin/test-net. |
| F5 | **Camoufox `headless="virtual"` + `block_images`** (Phase C1) | deep-dive | Likely improves hard-site bypass + speed. |
| F6 | **Obscura `--features stealth` image + canary benchmark** (Phase O0) | deep-dive | Unlocks obscura's real anti-bot value. |
| F7 | **Proxy rotation dimension** in `request_with_ladder` | prior plan | IP-reputation layer the ladder lacks (TLS-first premise stays). |
| F8 | **Crawl/map primitives** (native, MIT) | prior plan (Firecrawl gap) | `obscura scrape` can back the batch side (O1). |
| F9 | **E1→E2 escalation gate** (interactive-flagged only) | prior plan (Stage 8) | Behavioral change to both cascades; own PR. |
| F10 | **Real E1-via-Obscura-CDP** (Crawl4AI `BrowserConfig(cdp_url=...)`) | prior plan | Makes E1 actually render through obscura. |
| F11 | **Expose Camoufox targeting knobs** (os/screen/locale/addons) (Phase C2) | deep-dive | Caller-tunable realism. |
| F12 | **Obscura markdown / Fetch-interception paths** (O1/O2) | deep-dive | Cheap render + faster loads. |
| F13 | Flow bugs: EU decimal-comma 100×; `_looks_like_block` string heuristic; resolver `ValueError`→`ConfigurationError` | prior plan | Real but small/independent. |
| F14 | **Dependency upgrade sweep** (browser-use 0.13.x, crawl4ai) | prior plan | Do behind the pin + the launch()-contract test-net now in place. |
| F15 | Per-keystroke `humanlike_type` for browser-use typing (needs custom Controller) | prior plan | Low priority; browser-use owns its typing. |

### Suggested sequencing
1. **F3 + F5** (Camoufox correctness/stealth quick wins) — small, likely make E2/Yad2
   pass cleanly.
2. **F1** (Camoufox Pattern-D render + challenge detection) — the architectural payoff.
3. **F2** (learn-once/replay) on top of F1.
4. **F4 + F14** (upgrades) behind the test-net.
5. **F6 → F10/F12** (obscura stealth build, then leverage) as a track of its own.
6. **F7, F8, F9, F13** as independent smaller efforts.
