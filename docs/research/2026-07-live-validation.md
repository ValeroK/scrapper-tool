# Live cascade validation — 2026-07

Recorded after the Phase E dependency sweep (browser-use 0.5.9 → 0.13.6, curl_cffi
0.15 ladder refresh, mypy 2.3, crawl4ai 0.9.2). Every result is a **real fetch**
from this machine, not a fixture, run tier-by-tier so tier selection is
observable rather than assumed.

## Why these targets

Each site exercises a different cascade decision. The point of the table is the
*decision*, not the byte count.

| Category | What it should prove |
|----------|----------------------|
| clean-static | tier 1 (HTTP ladder) wins; render is a no-op |
| json-ld / SPA | render adds real content a raw fetch didn't have |
| protected | render clears a wall the ladder 403'd on — the tier's whole reason to exist |

## Results

`real` = the challenge classifier's `has_real_content` verdict (content-first: a
403 carrying a genuine DOM is a success, a bot-walled 200 is not). `vendor` = the
detected interstitial vendor, or `-`.

| Site | Ladder | Render | Read |
|------|--------|--------|------|
| books.toscrape | 200, 51 KB, real | 200, 51 KB, real | tier 1 wins; render redundant ✓ |
| quotes.toscrape | 200, 11 KB, real | 200, 11 KB, real | tier 1 wins ✓ |
| example.com | 200, 559 B, real | 200, 559 B, real | tier 1 wins ✓ |
| webscraper.io test-site | 200, 24 KB, real | 200, **54 KB**, real | render adds JS-built content (2.2×) |
| **one.co.il** | 200, 419 KB (unhydrated) | 200, **2.08 MB**, real | the headline: 5× real content from render |
| scrapingcourse (CF Turnstile) | 200, `cloudflare`, **not real** | 200, `cloudflare`, **not real** | honest: headless can't clear it here — see below |
| **store.mopar.com** | 403 all 5 profiles | **403, 1.35 MB, real** | Akamai 403s the doc, JS clears it, render gets the real DOM |
| **g2.com** | 403 all 5 profiles | **403, 422 KB, real** | Camoufox cleared Cloudflare |
| nowsecure.nl | 200, 180 KB, real | 200, 180 KB, real | ladder already passes |

## What this establishes

- **The render tier does its job.** mopar (Akamai) and g2 (Cloudflare) both 403 on
  every TLS profile, and a single Camoufox render turns each into real content —
  1.35 MB and 422 KB respectively. That is the exact case the tier exists for, and
  it works end-to-end on the upgraded stack, so browser-use 0.13's dependency pins
  did not disturb Camoufox.
- **The classifier is content-first and correct on every row.** mopar and g2
  render under a 403 yet report `real=True` (status is not the signal); a
  bot-walled response reports `real=False` regardless of its 200/403. No row is
  misclassified.
- **`chrome146` (the refreshed primary) wins every ladder 200.** The TLS refresh
  is healthy.

## The honest failure, and what it tells us

**scrapingcourse's Cloudflare Turnstile is not cleared** — `cloudflare` / `not
real` on *both* the ladder and the headless render. This is the right outcome to
record, not to hide:

- Headless Camoufox on this machine's (datacenter-class) IP hits the interactive
  Turnstile challenge, and the classifier refuses to pass the challenge page off
  as content.
- The escalation from here is **E2** (interactive agent, `headless=virtual`) or a
  **residential proxy** — the same conclusion Yad2 and mopar's earlier
  IP-throttled run reached. It is an IP-reputation / interactivity limit, not a
  code fault.

The measurable lesson across this whole run: the code cannot manufacture IP trust.
Everything the toolkit controls — TLS profiles, render, classification, tier
selection — behaves correctly; the remaining hard cases are the ones that need a
dimension (residential IP, human-like interaction) the local machine can't supply.

---

# Live validation run — 2026-08

Post-release verification of **2.1.0** (`main` @ `4f4ad1a`), the first run to put
the cookie threading, `doctor`, the CORS fix and the captcha cascade against real
sites — PRs #25 and #28 were built in a container with no general egress, so none
of it had ever touched a live host.

Host: Windows 11, Python 3.13.11, Camoufox 152.0.4-beta.28, datacenter-class
egress (Tel Aviv). Targets are new to the suite and deliberately chosen in
verticals with no personal account, one probe per target per tier.

## What passed

| Check | Result |
|---|---|
| `doctor` exit code agrees with reported status | `degraded` → exit 1 ✓ |
| Camoufox persistent-context render (**P2**) | 200, 38 KB, no `AttributeError` ✓ |
| Cookie jar write → reload → authenticated scrape | `cookies_applied=["a_b_c"]`, authed 15438 B vs anon 15517 B ✓ |
| Cookie value never echoed into the response | ✓ |
| Wildcard CORS does **not** grant credentials | `allow-origin: *`, `allow-credentials: absent` ✓ |
| Explicit origin list still gets credentials | `allow-credentials: true` ✓ |
| Non-listed origin not reflected | ✓ |
| `cookies` field without API key | 403 ✓ (and a no-cookies request still 200) |
| Unit suite | 1056 passed, 1 skipped (rookiepy absent) |

The **CORS fix is confirmed live** — the vulnerability 2.1.0 shipped to close is
closed, and nothing legitimate was lost.

## Site results

`vendor` = `_challenge.is_interstitial`; `real` = `has_real_content`. The ladder
column reports the profile that won, or the last one tried when all five failed.

| Site | Ladder | Render (Camoufox) | Read |
|---|---|---|---|
| seloger.com | **200**, 546 KB, real (chrome146) | — | DataDome 403 to plain curl; **TLS ladder cleared it** |
| homedepot.com | **200**, 1.30 MB, real (chrome146) | — | Akamai 403 to plain curl; **ladder cleared it** |
| vinted.com | 200, 1.93 MB, real (chrome146) | — | DataDome in monitor mode; ladder passes |
| hermes.com | 200, 530 KB, real (chrome146) | — | CF+DataDome stacked; ladder passes |
| **g2.com** | 403, 1.7 KB, `datadome` | **403, 799 KB, real** | render clears it; **vendor changed** — see below |
| **store.mopar.com** | 403, 6.0 KB, `cloudflare` | **403, 1.13 MB, real** | render clears it; **vendor changed** — see below |
| **bunnings.com.au** | 403, 6.1 KB, `cloudflare` | **403, 702 KB, real** | Camoufox cleared Cloudflare |
| dickssportinggoods.com | 200, 2.4 KB, **real=True (wrong)** | 200, 330 KB, real | **classifier bug — see below** |
| leboncoin.fr | 403, 771 B, `datadome` | 403, 1.5 KB, `datadome` | not cleared; correctly reported |
| shutterstock.com | 403, 787 B, `datadome` | 403, 1.5 KB, `datadome` | not cleared; correctly reported |
| gamestop.com | 403, 5.5 KB, `cloudflare` | 403, 5.3 KB, `cloudflare` | not cleared; correctly reported |
| kmart.com.au | 403, 374 B, `unknown` | 403, 294 B, `akamai` | not cleared; vendor named only on render |
| ticketek.com.au | 403, 6.0 KB, `unknown` | 403, 5.8 KB, `unknown` | not cleared; vendor never named |

### The TLS ladder is doing real work

`seloger.com` and `homedepot.com` both **403 a plain curl** and both return full
pages under `chrome146`. That is the impersonation ladder earning its place, and
it is worth noting neither needed a browser at all.

### DataDome signatures fired for the first time

`_challenge.py:65` has carried DataDome markers since it was written; the 2026-07
run only ever saw Cloudflare and Akamai, so they had never matched a real
response. They now match on `leboncoin.fr`, `shutterstock.com` and `g2.com`, via
`geo.captcha-delivery.com` in the interstitial body. That branch is live-verified.

### Two targets changed vendor since 2026-07 — the classifier is right, the old rows are stale

- **store.mopar.com migrated Akamai → Cloudflare.** Now `Server: cloudflare`,
  `cf-ray` present, body `<title>just a moment...`. The 2026-07 row calling it
  Akamai is simply out of date.
- **g2.com added DataDome on top of Cloudflare.** `Server: cloudflare` *and*
  `x-datadome: protected`, with a `geo.captcha-delivery.com` body. The classifier
  names `datadome`, which is the vendor actually serving the challenge — correct.

Both still render to real content (799 KB and 1.13 MB), so the 2026-07
regression targets **pass**.

## Bugs found

### 1. A bot-walled HTTP 200 is scored as real content (high)

`www.dickssportinggoods.com` returns **200** with a 2,373-byte body that is one
script tag plus a hidden `sec-if-cpt-container` div — an Akamai Bot Manager
interstitial. The classifier reports `is_interstitial=None`, `has_real_content=True`.

Root cause: `has_real_content()` is defined as `is_interstitial(...) is None`
(`_challenge.py:225`), and `is_interstitial` only falls back to `"unknown"` when
the status is in `{403, 503}` (`_challenge.py:129`). **A wall served with status
200 and no matching vendor signature is invisible.**

The same URL rendered through Camoufox returns **330 KB** — a 140x difference
against the 2.4 KB the ladder accepted as real. So the cascade stops escalating
and hands the caller a bot wall as content. This is the exact failure the module
docstring warns about ("a bot-walled 200 is a failure").

### 2. Akamai signatures are unreliable across page variants (medium)

`("reference #18.", "akamai reference", "ak_bmsc_challenge")` matched kmart's
*render* body (294 B) but not its *ladder* body (374 B, title "access denied"),
and matched nothing on the Akamai sensor page above. 403 misses degrade safely to
`"unknown"`; the 200 miss does not.

### 3. `browser_binary_present("camoufox")` is a false negative on every install (medium)

`_extras.browser_binary_present` reads `camoufox.path`, which does not exist (the
real API is `camoufox.pkgman.get_path`), then falls back to globbing the
**Playwright** browser root — where Camoufox never installs (it uses
`%LOCALAPPDATA%/camoufox/...`). Verified: Camoufox 152.0.4-beta.28 renders
200/38 KB while the probe returns `False`.

Consequences: `doctor` reports `render: degraded` and tells operators to run
`camoufox fetch` when they already have; and `check_render_persistent_context` in
`scripts/e2e/test_cookies_live.py` **SKIPs**, so the P2 regression check silently
never runs. Same bug class as the `user_data_dir_supported()` false negative fixed
in PR #25.

## CAPTCHA: what we can pass, and how

### Widget detection works; the interstitial case never reaches a solver

| Page | `detect_challenge` result |
|---|---|
| 2captcha demo (`3x…FF`) | `('turnstile', '3x00000000000000000000FF')` |
| Google reCAPTCHA v2 demo | `('recaptcha-v2', '6Le-wvkS…')` |
| hCaptcha demo | `('hcaptcha', 'a5f74b19…')` |
| nopecha CF interstitial | **`None`** — title "Just a moment…" |

Detection and sitekey extraction are correct for all three supported kinds. But a
Cloudflare **JS interstitial is not a captcha** — there is no widget, so
`detect_challenge` returns `None` and the solver cascade is never invoked. Tier 0
and Tier 1 both "failed" `nopecha.com/demo/cloudflare` because **neither was ever
called**, not because they solved badly. That distinction matters: the
interstitial case is a fingerprint/IP problem, not a captcha problem.

### Headless vs headful: the limit is IP, not headlessness

Re-ran every target headless Camoufox could not clear, with `headful=True` (a real
visible window — `headless="virtual"` is Xvfb and cannot run on Windows):

| Site | Headless | Headful |
|---|---|---|
| gamestop.com | 403, 5328 B, `cloudflare` | 403, **5328 B**, `cloudflare` |
| leboncoin.fr | 403, 1468 B, `datadome` | 403, **1468 B**, `datadome` |
| kmart.com.au | 403, 294 B, `akamai` | 403, **294 B**, `akamai` |
| ticketek.com.au | 403, 5814 B, `unknown` | 403, **5814 B**, `unknown` |

**Byte-for-byte identical.** The challenge decision is made before any
headless/headful difference could matter. No local lever — headful, virtual
display, behaviour tuning — moves these; the only remaining dimension is a
residential/mobile IP. This is a controlled confirmation of the 2026-07
conclusion rather than a restatement of it.

### Most captcha demo pages prove nothing

Cloudflare publishes dummy test sitekeys, and the popular demos use them:

| Demo | Sitekey | Documented behaviour |
|---|---|---|
| `demo.turnstile.workers.dev` | `1x00000000000000000000AA` | **Always passes** |
| `2captcha.com/demo/cloudflare-turnstile` | `3x00000000000000000000FF` | **Forces interactive challenge** |

A pass against `1x…AA` is a **false positive** — that key issues a token to any
client, including a naive bot. Even `3x…FF` always issues a token; it proves the
tool can drive the interactive widget, not that it beats real Cloudflare bot
detection. Any future captcha result must record the sitekey and widget mode, or
it is uninterpretable.

### 7 of 10 declared captcha kinds are unreachable

`_DETECT_JS` (`captcha_dom.py:41-64`) has exactly three return paths, so
`detect_challenge` can only ever yield `turnstile`, `hcaptcha`, `recaptcha-v2`.
`CaptchaKind` (`captcha.py:39`) declares ten. `arkose`, `aws-waf`, `datadome`,
`funcaptcha`, `geetest`, `image` and `recaptcha-v3` **cannot be produced by any
automatic path**, and `solve_on_page` is the only caller of `solver.solve(kind,…)`.
The paid tiers' advertised coverage of DataDome/AWS-WAF/Funcaptcha is therefore
unreachable in practice, which matters directly: the three DataDome walls above
could never have been routed to a solver even with a key.

### Where a local VLM can and cannot help

LM Studio (`127.0.0.1:6543`) is now the configured default. Two vision models are
installed — `google/gemma-4-e4b` (loaded) and `qwen/qwen3.6-27b`.

- **Turnstile / Cloudflare interstitials — a VLM cannot help.** No visual puzzle
  exists; the token comes from Cloudflare's own attestation, and the headful
  experiment above shows the decision is IP-driven.
- **reCAPTCHA v2 and hCaptcha — genuinely VLM-shaped**, and detection already
  returns valid sitekeys for both, so the foundation is in place. The missing
  piece is only the solving step.

Two prerequisites before any such work:

1. **`is_vision_model()` is broken for every locally installed VLM.** It matches
   only the substrings `vl`, `vision`, `llava`, `minicpm-v` (`llm.py:238`), so
   both `google/gemma-4-e4b` and `qwen/qwen3.6-27b` return `False` — while LM
   Studio reports both as `type=vlm`, and gemma correctly identified a generated
   test image as "Red". `browse.py:180` therefore disables vision for models that
   demonstrably see, meaning E2 browse mode has been running blind. Prefer
   querying `/api/v0/models` for `type == "vlm"` over pattern-matching names.
2. **`gemma-4-e4b` is a reasoning model.** A 20-token budget returned
   `content: ""` with `finish_reason: "length"` — the entire budget went to
   `reasoning_content`. At 400 tokens it answered immediately. A solver expecting
   a terse `"tiles: 1,3,5"` will cap tokens low and misread starvation as solver
   failure.

## Docker round-trip — verified against the published image

Run against `ghcr.io/valerok/scrapper-tool:2.1.0` (the artifact users actually
pull), not just a local build:

| Check | Result |
|---|---|
| `cookies export` inside the container | **exit 3**, with the host-side pointer message ✓ |
| `POST /scrape` with `cookies` + `X-API-Key` | 200, `cookies_applied=["a_b_c"]`, **15441 B** ✓ |
| Cookie value echoed in the response | no ✓ |
| `Origin:` under default wildcard CORS | `allow-origin: *`, **no `allow-credentials`** ✓ |
| `doctor` inside the container | `degraded` ✓ |

The 15441 bytes matches the authenticated size measured directly with curl, so
the cookie demonstrably took effect through the containerised REST path.

Note on the 403 gate: with `SCRAPPER_TOOL_HTTP_API_KEY` **set**, a keyless
request is rejected `401` by auth before the cookies gate is reached. The `403`
is the *no-key-configured* case, verified separately in-process. Both are correct.

**Runbook correction:** the documented post-release command
`docker run --rm ghcr.io/valerok/scrapper-tool:2.1.0 scrapper-tool doctor --json`
does **not** work — the image `ENTRYPOINT` is `scrapper-tool-serve`
(`Dockerfile:174`), so those tokens are parsed as server flags and it exits 2.
Use `docker run --rm --entrypoint scrapper-tool ghcr.io/... doctor --json`. (This
form appears only in the release runbook, not in the repo docs.)

## Q3 answered: browser-use 0.13 over CDP *does* reuse the default context

The handoff framed Q3 around `storage_state`, but the shipped code deliberately
does not use it. `browse.py:185-191` rejects it as "a *launch* argument" that
"would at best seed a fresh context that nothing then drives", and instead calls
`_inject_cookies()` on the resolved live context before browser-use attaches.

Measured directly (launch Chromium with CDP, inject into the live context, then
attach a CDP client). Anonymous and authenticated `/login` differ by ~73-76 bytes
consistently, which discriminates the two states:

| Path | Bytes | State |
|---|---|---|
| Page created on the **launch side**, injected context | **15378** | authenticated |
| CDP client **reusing the existing target** | **15378** | authenticated |
| CDP client creating a **new page** | 15451 | anonymous |

**Verdict: the shipped approach is correct.** Cookies injected into the live
context reach a CDP-attached driver — *provided it drives the existing target*.
The caveat is precise and worth keeping: if browser-use ever opens a **new**
page/tab instead of attaching to the existing one, that page lands in a fresh
context and the session is silently lost. Worth a regression test pinning the
"attach to existing target" assumption, since it is browser-use's behaviour and
not something this repo controls.

---

# Fixes — 2026-08-12

Everything below was fixed after the run above, with the measurements that drove
each decision. Several "obvious" fixes turned out to be wrong when tested; those
are recorded too, because the wrong version is the one a reader would otherwise
reach for.

## Bug 1 + Bug 2 were the same bug

`is_interstitial`'s vendor-signature loop already ran regardless of status. The
200 wall was invisible because **no signature matched**, not because of the
`{403,503}` gate — the gate is what rescued the 403 variants. So the "200-status
fallback" framing was wrong: fixing signature coverage fixes both.

Three bodies captured live from `www.dickssportinggoods.com`, all from one host
on one day, with the **TLS profile alone** deciding which you get:

| Fixture | Profile | Status | Bytes | What it is |
|---|---|---|---|---|
| `akamai_behavioral_200.html` | `chrome124` | 200 | 2,365 | tile-challenge wall |
| `akamai_maintenance_403.html` | `chrome` | 403 | 1,571 | "Oops, Something Went Wrong" soft block |
| `akamai_protected_real_shell_200.html` | `chrome` | 200 | 3,406 | **the real Angular shell** |

They live in `tests/fixtures/challenge/` and are the calibration set.

**The trap.** The obvious Akamai signature is the sensor `<script src>` — a
random-looking path with a `?v=<uuid>`. It is present on the wall *and on the
good Angular shell*, along with a `sec-overlay` / `sec-container` pair. Matching
either would flag every Akamai-protected page on the internet as a wall. The
signatures added are the challenge-only container ids: `sec-if-cpt-container`,
`sec-bc-tile-container`, `sec-bc-text-container`, `scf-akamai-protected-by`.

The 403 "maintenance" variant is **honestly un-attributable** — its only
Akamai-specific content is that same sensor script. It stays `unknown` via the
status gate, which is correct, and there is a test pinning that we did not
"fix" it by reaching for the sensor path.

### The content-free-shell fallback, and what it cannot do

A novel 200 wall with no signature would still be invisible, so
`looks_like_content_free_shell` is the net under it. The obvious heuristic —
"small body, almost no visible text" — **does not work**: it flags the legitimate
Angular shell, whose only text is a `<noscript>` line. The discriminator that
actually separates them is the `<title>`: real documents have one (the shell's is
"DICK'S Sporting Goods - Official Site"), while the wall ships bare
`<html lang="en"><body>` with no `<head>` at all.

It then needed a second guard. Flagging on titleless-and-textless alone broke a
real cascade test: a page whose entire payload is a **JSON-LD block** with a
JS-filled `<body>` has no title and no visible text either, and marking it as
walled made the cascade skip Pattern D. Structured data (`application/ld+json`,
microdata, Open Graph) is now an explicit escape hatch — a bot wall does not
publish a schema.org Product.

Live re-verification, one host, three profiles:

| Profile | Status | Bytes | vendor | real |
|---|---|---|---|---|
| `chrome124` | 200 | 2,364 | **akamai** | **False** |
| `chrome` | 403 | 1,571 | unknown | False |
| `safari18_0` | 403 | 1,571 | unknown | False |

`example.com`, `books.toscrape`, `quotes.toscrape` all still `vendor=None`,
`real=True`.

## Bug 3 + 4: the browser probes had a deeper root cause

`playwright_browsers_root()` returned `~/.cache/ms-playwright` unconditionally —
the **Linux** default. On this machine the real root is
`%LOCALAPPDATA%\ms-playwright` (holding `chromium-1208/`, `firefox-1509/`) and
the Linux path does not exist, so *every* binary probe was false on Windows
before any glob was even consulted. Fixing the globs alone would have changed
nothing. The root is now platform-aware, and the glob table covers Windows
(`chrome-win64/chrome.exe`, `firefox/firefox.exe`) and macOS layouts.

Camoufox additionally installs **outside** the Playwright root entirely
(`%LOCALAPPDATA%/camoufox/...`). `camoufox.path` does not exist on any release;
`pkgman.launch_path()` is the real API and returns the executable directly.

Measured before → after on this machine, all three were `False`:

| Probe | Before | After |
|---|---|---|
| `camoufox` | False | **True** |
| `patchright` | False | **True** |
| `scrapling` | False | **True** |

`doctor` now reports `render: ok` ("camoufox module + binary present") and
`d: ok` instead of `degraded`, and the P2 e2e check no longer silently SKIPs.

Note this surfaced as *test failures*: three tests asserted `False` for an empty
`tmp_path` root and only passed because the probe was broken. They now hide the
`camoufox` module to isolate the on-disk question they actually ask.

## Bug 5: `is_vision_model`

Name matching is the wrong mechanism, so it is now the fallback rather than the
answer. `supports_vision(model, base_url)` asks LM Studio's `/api/v0/models` for
the declared `type`, and degrades to the name heuristic when the endpoint is
absent (plain llama.cpp / vLLM / Ollama), unreachable, or silent about the model.

| Model | Name heuristic | Probed |
|---|---|---|
| `google/gemma-4-e4b` | True (after widening) | **True** |
| `qwen/qwen3.6-27b` | False | **True** |
| `openchat-3.5-7b-qwen-v2.0` | False | False |
| `text-embedding-nomic-embed-text-v1.5` | False | False |

`browse.py` now awaits this, so E2 stops running blind. The heuristic itself also
grew the families it plainly missed (gemma-3/4, pixtral, internvl, moondream,
idefics).

## CAPTCHA: from 3 reachable kinds to 10

This was the substantive gap. `_DETECT_JS` had three return paths against ten
declared `CaptchaKind` values, so seven could not be produced by any automatic
path — the paid tiers' advertised DataDome / AWS-WAF / FunCaptcha coverage was
unreachable in practice.

**Detection alone would not have been enough.** Two more layers were broken
underneath it, and neither had ever been exercised because nothing could reach
them:

1. **`solve_on_page` never passed `extra`.** The `solve(kind, site_key, url)`
   signature assumes every challenge is identified by a sitekey. DataDome needs
   the challenge URL, AWS WAF needs the `gokuProps` triple, GeeTest needs a
   nonce, and an image captcha needs pixels. `extra` existed on the protocol and
   was never populated.
2. **The provider payload keys were wrong.** Every kind was sent as
   `websiteKey`. CapSolver wants `websitePublicKey` for FunCaptcha, `captchaUrl`
   for DataDome, the `aws*` fields for AWS WAF, and base64 `body` (with no
   `websiteURL`) for image. 2Captcha has the same trap with `publickey` / `gt` /
   `body`. Those tasks would have been rejected by the API even with a correct
   kind and a valid key.

Also fixed: `AutoCascadeSolver.supported` was hard-coded to the four free-tier
kinds, so a caller checking it would conclude a paid cascade could not handle
DataDome when the CapSolver tier in it can. It is now the union of its tiers.

### Live detection results

Driven through real Camoufox against live pages, before → after:

| Page | Before | After |
|---|---|---|
| 2captcha Turnstile demo | `turnstile` + key | `turnstile` + key |
| Google reCAPTCHA v2 demo | `recaptcha-v2` + key | `recaptcha-v2` + key |
| hCaptcha demo | `hcaptcha` + key | `hcaptcha` + key |
| 2captcha reCAPTCHA **v3** demo | `recaptcha-v2`, **empty key** | **`recaptcha-v3`** + `6Lcyqq8o…` |
| 2captcha **GeeTest v4** demo | *(unreachable kind)* | **`geetest`** + `e392e1d7fd42…` |
| nopecha CF interstitial | `None` | `None` (correct — see below) |

Two findings worth keeping:

- **reCAPTCHA v3's invisible badge also renders an `api2/anchor` iframe.** Testing
  that iframe before the `render=` script parameter misreported every v3 page as
  v2 with an empty sitekey. Ordering is now explicit-widget → `render=` → bare
  iframe, and the anchor iframe's `k=` is used to recover a v2 key when the host
  renders the widget programmatically.
- **GeeTest's captcha id is in no global.** On a live v4 page the only place it
  appears is the loader's query string (`gcaptcha4.geetest.com/load?…&captcha_id=`).

`2captcha.com/demo/arkoselabs` **serves no Arkose resources at all** — verified
directly, including after clicking through the form; the page loads only a chat
widget. So Arkose/FunCaptcha detection is fixture-verified, not live-verified,
and that distinction should survive into any future run.

### Fixture-verified kinds

`tests/unit/test_captcha_detection_dom.py` runs the **real** `_DETECT_JS` in a
real browser against 16 markup cases (the previous suite fed canned dicts to a
fake page, so the detector itself was untested — which is how it shipped with
three branches). It covers all ten kinds plus three negatives.

Two real bugs came out of writing it:

- **`window.gokuProps` outlives its document.** Keying AWS-WAF detection on the
  global's mere presence made every subsequent page in that tab report `aws-waf`,
  swallowing the DataDome and image branches. It now requires a real `awswaf`
  resource or a fully-populated triple.
- **`new URL(relative, 'about:blank')` throws**, and an uncaught throw aborted the
  *entire* detection — one relative `<img src>` on a document with a
  non-hierarchical base made the page look captcha-free. URL resolution now
  degrades to the raw attribute.

### What still cannot work, and why

Unchanged from the run above, and no amount of detection changes it: a Cloudflare
or DataDome **JS interstitial is not a captcha**. There is no widget, so
`detect_challenge` correctly returns `None`. The headless-vs-headful table is
byte-for-byte identical on all four sites, so the decision is IP-driven.

What did change is that the old code returned immediately on `None`, which meant
the stealth auto-pass tier was **never invoked** for the commonest wall on the
web — the run above recorded tier 0 and tier 1 "failing" a CF interstitial when
neither had been called. `solve_on_page` now recognises a widget-less
interstitial and settles/reloads it, which is the mechanism that actually clears
these, and re-reads the document rather than trusting "no widget found" as proof
the wall is gone. When it does not clear, the answer is still an honest "not
handled" and the caller escalates.

**"Captcha working on all cases" is therefore not achievable and should not be
claimed.** What is now true: all ten declared kinds are reachable, routable, and
built into payloads the providers accept; the interstitial case reaches the
stealth tier instead of being skipped; and the remaining failures are IP
reputation, which needs a residential/mobile egress, not code.

---

# Captcha solving — 2026-08-15

The spike, plus the two free tiers that had to exist before it made sense. Every
number below is a live run against `google.com/recaptcha/api2/demo` from this
machine's datacenter-class IP.

## The cascade now has four tiers

| Tier | Mechanism | Cost | Covers |
|---|---|---|---|
| 0 | settle + reload | free | Turnstile, JS interstitials |
| 0.5 | **click the checkbox** | free | reCAPTCHA v2, hCaptcha |
| 1 | **local VLM grid solve** | free | reCAPTCHA v2, hCaptcha image grids |
| 2 | paid solver token / cookie | per solve | the rest |

Tiers 0.5 and 1 are new. So is the thing that makes any of them measurable:

**Success was being read off the wrong signal.** `solve_on_page` returned `True`
unconditionally after injecting a token, and `_settle_and_recheck` asked "is the
widget gone". A solved reCAPTCHA or hCaptcha **keeps its widget** — it just turns
green — so a real success read as a failure, while a foreign Turnstile token
that failed its environment check (the normal outcome) read as a *success* and
stopped the cascade. Both now key off the response field
(`g-recaptcha-response` / `h-captcha-response` / `cf-turnstile-response`), with
widget-absence kept only for interstitials, which have no response field.

## The checkbox tier works, and its failure is the useful part

Live, both anchor iframes are found and the click lands. Neither passed outright
on this IP — and what happens instead is exactly the handoff tier 1 needs:

```
checkbox clicked -> aria-checked stays "false"
                 -> bframe appears: "Select all images with crosswalks"
```

That is not a bug. On a residential IP the checkbox frequently *is* accepted, and
this tier costs one click. Before it existed, reCAPTCHA and hCaptcha skipped
tier 0 entirely (`CamoufoxAutoSolver.supported` is `{"turnstile"}`) and went
straight to a paid solver — paying for challenges a click can clear.

## The grid solver: pipeline works, the local model does not

End-to-end the plumbing is sound — checkbox -> bframe -> screenshot -> VLM ->
parse -> click tiles -> verify -> read token. Two real bugs were found by running
it rather than by reading it:

- **The prompt renders as two lines.** `.rc-imageselect-desc` innerText is
  `"Select all squares with\nmotorcycles"`, so taking the first line reduced the
  target to `"a"` and the model was asked to find nothing. The subject has its
  own `<strong>` — read the element, don't regex the joined text.
- **The panel screenshot was not the grid.** `#rc-imageselect` includes the
  instruction banner and the reload/audio/SKIP footer, so the 16 tiles filled
  ~70% of an image the model was told was a 4x4 grid numbered 1-16 — it had to
  guess where the grid began before it could number anything. Screenshotting the
  table element frames it exactly.
- **512 tokens starved the model.** Against a 16-tile grid `gemma-4-e4b` spent an
  entire 512-token budget on `reasoning_content` and returned no answer at all,
  three rounds running. Grid solves now get 2048.

With all three fixed, replies are clean and parseable. They are also wrong:

| Grid | Target | Reply | Verdict |
|---|---|---|---|
| 3x3 | `bus` | `2,5,8` | 2 right, tile 8 is a **fire hydrant**, missed the obvious bus in 6 |
| 4x4 | `buses` | `6,7,8,11` | 3 right, tile 8 is a **box truck**, missed the bus front in 10 |

Measured against reCAPTCHA's own verify button, so no eyeballing is involved:

| Model | Result |
|---|---|
| `google/gemma-4-e4b` | **0 / 5 solved** (~62 s per attempt) |
| `qwen/qwen3.6-27b` | **untestable** — HTTP 400, "insufficient system resources" |

The failure mode is consistent: it finds *some* of the right tiles and adds a
confident false positive. reCAPTCHA is all-or-nothing, so near-misses score zero.

**Conclusion: keep the tier, do not rely on it.** It costs nothing when it fails,
it returns an honest `False` so the cascade escalates, and the same plumbing is
what a stronger model (or a hosted VLM) would use unchanged. What it is not is a
replacement for a paid solver on reCAPTCHA today. A rerun on hardware that can
hold a 27B+ VLM is the obvious next measurement, and the benchmark harness makes
it a one-command job.

---

# Model selection and the slider tier — 2026-08-15

## The 0/5 was the model, not the pipeline

`qwen/qwen3.6-27b` was recorded above as "untestable — insufficient system
resources". That was **wrong, and worth correcting precisely**: the machine has
an RTX 3090 (24 GB) and 32 GB RAM, and the weights are 16.28 GiB. What blew past
VRAM was the model's **262,144-token default context**, not the model. Loading it
with `--context-length 8192` succeeds in 11 seconds.

With that one change, the same harness against the same live target:

| Model | On disk | Solved | Avg |
|---|---|---|---|
| `google/gemma-4-e4b` | 6.33 GB | **0 / 5** | 62 s |
| `qwen/qwen3-vl-8b` | 6.19 GB | **1 / 5** | 48 s |
| `qwen/qwen3.6-27b` | 17.48 GB | **5 / 5** | 72 s |
| `qwen/qwen3.8-27b` | 17.74 GB | **4 / 5** | 73 s |

Tokens on the wins were 2254-2382 chars — real reCAPTCHA tokens, read back off
the response field, with reCAPTCHA's own verify button as the arbiter.

**So the grid solver does work.** The earlier conclusion ("keep the tier, do not
rely on it") was right about `gemma-4-e4b` and wrong as a general statement. The
correction that matters for anyone reading this: *check the context length before
concluding a model does not fit.*

`qwen3.8-27b` (multimodal, `mmproj` vision projector, MathVision 94.6) matches
`qwen3.6-27b` within the noise of a five-run sample — 4/5 against 5/5 is one
dropped attempt, not a ranking.

On "lightweight": both ~6 GB models fail. Qwen3-VL-8B is purpose-built for
spatial reasoning and still only managed 1/5, so this is not a matter of picking
a better small model — the task needs the capability that arrives around 27B.
Sample sizes are five runs each; the 0 / 1 / 5 spread is wide enough to act on,
but it is not a precise success rate.

## Slider captchas: solved, and no model needed at all

`geetest` and `datadome` are gap-alignment puzzles with an exact answer, so they
are geometry rather than perception. It works, and the first sample overstated
how well: **3 accepted out of 14 live attempts (~20%)**, each showing
"Verification Success". The initial 2/4 was too small a sample to publish, and
saying so is the point — the honest aggregate is the useful number.

Gap *detection* is not the limiting factor. After a race fix (below) the puzzle
opens and drags **6/6**; what varies is whether GeeTest believes the drag. The
remaining failures look like trajectory scoring and IP reputation — the same
dimension every other unsolved case in this investigation runs into, and this
demo had been hit ~20 times from one address by then.

### A race that looked exactly like flakiness

The canvas *elements* appear before anything is painted into them, and reading
them in that window is indistinguishable from "there is no puzzle": the piece
canvas is still fully transparent, so `piece_bounds` returns None, and the
background still equals `fullbg`, so the diff finds no notch. Both detectors
correctly returned None and the solver declined a puzzle that was about to exist.

Three consecutive attempts on one target gave `piece=None, gap=None` twice and a
clean `GapMatch(x=149, confidence=1.0)` once. Re-running the matrix is what
caught it; reading the code would not have. `solve_slider` now polls ~4 s for the
canvases to paint.

The primary method is a diff, not a match: GeeTest ships `geetest_canvas_bg`
(notched) *and* `geetest_canvas_fullbg` (intact). Where they differ is the notch.
`fullbg` renders at 0x0 so it cannot be screenshotted, but the canvas still holds
its pixels and `toDataURL` reads them — verified untainted.

Four bugs, none of which a synthetic test would have caught:

- **Correlating the piece's texture does not work.** The piece is a crop of the
  photo it sits in, so it correlates about equally everywhere; a random-noise
  "piece" scored 0.57 and the search always returned the last valid offset.
- **The piece canvas is the same size as the background** (260x160), almost
  entirely transparent. A canvas-width check therefore rejected every real input.
  The alpha bounding box is the piece: measured (0, 40) = 41 px.
- **`.geetest_btn` is the radar button, not the drag handle** — 300 px wide
  against the real handle's 66 px, so every drag started from the middle of the
  wrong element.
- **The gap is in canvas pixels, the drag is in CSS pixels**, and they differ
  (260 intrinsic vs 258 displayed). Applying the scale is what turned 0/4 into 2/4.

One measurement error is worth recording too: an earlier run reported "3/3
accepted" by checking whether the success element *existed*. It exists hidden on
every page. Checking that it is **visible and non-empty** turned that 3/3 into
0/3, which was the honest number at the time.

## What "all captcha types" can and cannot mean

| Kind | Local/free path | Status |
|---|---|---|
| turnstile | settle | works when the IP is not already burned |
| recaptcha-v2, hcaptcha | checkbox, then VLM grid | checkbox works; **grid solves 5/5 with `qwen3.6-27b`**, 0-1/5 with ~6 GB models |
| image | VLM OCR | routed, detection + base64 capture in place, unmeasured |
| geetest, datadome | slider CV | **built; 3/14 (~20%) live on GeeTest v3**, no model required |
| recaptcha-v3 | none possible | risk score; no puzzle exists |
| aws-waf | none possible | proof-of-work |
| funcaptcha, arkose | none practical | rotating 3D, beyond a small VLM |

Still open:

- **DataDome's slider is unverified live.** The solver handles it by the same
  path as GeeTest, but DataDome gates on IP before showing a puzzle, so no live
  attempt was possible from this egress. GeeTest v3 is the measured one.
- **The `image` (OCR) kind is routed but unmeasured** — detection and in-page
  base64 capture are in place, and nothing has been solved through it.
- Arkose/FunCaptcha live verification (needs a real Arkose-protected target;
  `2captcha.com/demo/arkoselabs` serves no Arkose resources).
- No paid-solver round trip has been exercised: the new kinds are verified as
  detected and correctly routed, not as *solved by a provider*, which needs a key.
- The interstitial wall remains an IP-reputation problem, unchanged.
- `_browser_cookies.py` remains unvalidated on Windows: Chrome and Edge both
  carry `app_bound_encrypted_key` (Chrome 127+ App-Bound Encryption), which no
  external process can decrypt, and no Gecko browser is installed. This run used
  a synthetic jar, which starts downstream of extraction.


---

# Full cascade re-run — 2026-08-15

Every target from both earlier runs, ladder first and render only where the
ladder did not produce real content, judged by the shipped classifier. Same
datacenter egress as before.

**16 / 20 scraped.** 9 by the TLS ladder alone, 7 by render, 4 genuinely walled.

| Verdict | Sites |
|---|---|
| **Ladder** (9) | example.com, books.toscrape, quotes.toscrape, webscraper.io, nowsecure.nl, one.co.il, homedepot.com, vinted.com, hermes.com |
| **Render** (7) | seloger.com, g2.com, store.mopar.com, bunnings.com.au, dickssportinggoods, leboncoin.fr, shutterstock.com |
| **Blocked** (4) | gamestop.com (cloudflare), kmart.com.au (akamai), ticketek.com.au (unknown), scrapingcourse CF (cloudflare) |

Two sites that were **blocked in 2026-08 now clear**: `leboncoin.fr` renders 861 KB
and `shutterstock.com` renders 1.02 MB under a 403 — the DataDome walls that
previously returned 1.5 KB interstitials. `dickssportinggoods` is also now
correctly handled at 378 KB rather than being accepted as a 2.4 KB wall.

One drifted the other way: `seloger.com` was a ladder win in 2026-08 and now 403s
all five profiles, so render carries it. Vendor configurations move; this is why
the row-level history matters more than any single pass rate.

`bunnings.com.au` first reported BLOCKED on a render **error**, not a wall —
running twenty browsers back to back under a 60 s cap. Retried alone it returns
403 with 664 KB of real DOM. Worth separating in any future harness: an
infrastructure failure and an anti-bot wall are not the same result, and the
first one silently understates the pass rate.

## What the four blocked sites have in common

Nothing that code reaches. They are the same IP-reputation cases this document
has recorded three times now — headless and headful returned byte-identical
responses on all of them, so the decision is made before any local lever applies.
A residential or mobile egress is the only remaining dimension, and
`config.proxy` is already wired through `browse.py` and `extract.py` waiting for
one.

**"Scrapes any website" is not the claim.** 80% of a deliberately hostile list,
with the failures understood and attributable, is.
