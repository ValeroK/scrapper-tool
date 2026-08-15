# Testing guide — every variant, and how to reproduce it

Three tiers of test, with different costs and different guarantees. Run them in
this order; each one is only worth running when the cheaper one above it passes.

| Tier | Command | Needs | Time |
|---|---|---|---|
| 1. Unit + contract | `uv run pytest -m "not live"` | nothing | ~2.5 min |
| 2. Real-browser DOM | `uv run pytest tests/unit/test_captcha_detection_dom.py` | Camoufox binary | ~20 s |
| 3. Live targets | `uv run python scripts/e2e/run_targets.py --category all` | network, and a VLM for solve | ~15 min |

Tier 3 hits real, commercially-protected sites. **Never wire it into CI** — see
[`scripts/e2e/targets.yaml`](../scripts/e2e/targets.yaml). `tests/canary_targets.yaml`
is the CI-safe list and contains no real vendors, deliberately.

---

## Tier 1 — unit and contract tests

```bash
uv run pytest -m "not live" --cov
```

Expected: **1201 passed, 1 skipped, coverage ~87.5%** (floor 85%).

One **known pre-existing failure**, unrelated to this work and failing on a clean
checkout of `main`:

```
tests/unit/test_crawl_endpoints.py::TestCrawlEndpoint::test_a_failing_page_does_not_fail_the_crawl
```

It drives a real browser at `site.test/missing` and depends on DNS behaviour.
The skip is `rookiepy` (OS credential store), absent on this machine.

### What the notable suites pin

| File | Guards against |
|---|---|
| `test_challenge.py` | Bot walls scored as content, **and** content scored as walls. Both directions, both measured. |
| `test_captcha_detection_dom.py` | The detector itself — runs the real JS in a real browser, 16 markup cases. |
| `test_captcha_vision.py` | Grid solver orchestration: rounds, empty replies, honest failure. |
| `test_captcha_slider.py` | Gap detection against **real** GeeTest canvases; drag realism. |
| `test_cookie_transfer.py` | Cross-domain leaks and stale-clearance replay. |
| `test_proxy_wiring.py` | A configured proxy actually reaching the fetcher. |

---

## Tier 2 — real-browser DOM tests

```bash
uv run pytest tests/unit/test_captcha_detection_dom.py -v
```

Runs the actual `_DETECT_JS` against 16 synthetic-but-faithful markup cases.
Skips cleanly with no Camoufox binary (`camoufox fetch` to install).

The rest of the captcha suite feeds canned dicts to a fake page, which is why the
detector shipped with three branches against ten declared kinds — nothing
executed the JS. This is the file that would have caught it.

---

## Tier 3 — live targets

```bash
uv run python scripts/e2e/run_targets.py --category cascade         # ~6 min
uv run python scripts/e2e/run_targets.py --category captcha-detect  # ~2 min
uv run python scripts/e2e/run_targets.py --category captcha-solve --attempts 5
uv run python scripts/e2e/run_targets.py --category cookies
```

Exit 0 when every attempted target met its recorded expectation. Targets needing
something absent (a VLM, a logged-in domain) report SKIP and do not fail — an
honest skip beats a green tick that proved nothing.

### Before running the solve category

```bash
lms load qwen/qwen3.8-27b --context-length 8192 --gpu max
```

**The context length is not optional.** The model's default is 262,144 tokens,
and that KV cache — not the 16 GiB of weights — is what overflows a 24 GB card.
Omitting it produces `insufficient system resources`, which reads as "this model
does not fit" and is how the model was written off for an entire session.

### Rate-limit yourself

Several targets degrade under repeated hits from one address. A GeeTest demo hit
~20 times in an afternoon measurably stopped accepting solves it had accepted
earlier, which makes your own results look worse than the code is.

---

## Results, as measured on 2026-08-15

Host: Windows 11, RTX 3090 24 GB, **datacenter egress (Tel Aviv)** — the least
favourable case, and the dominant variable in every failure below.

### Cascade: 16/20

| Verdict | Sites |
|---|---|
| **Ladder** (9) | example, books/quotes.toscrape, webscraper.io, nowsecure, one.co.il, homedepot (1.5 MB), vinted (1.9 MB), hermes |
| **Render** (7) | seloger, g2, store.mopar, bunnings, dickssportinggoods, leboncoin, shutterstock |
| **Blocked** (4) | gamestop (cloudflare), kmart (akamai), ticketek (unknown), scrapingcourse (cloudflare) |

The four blocked are IP-reputation cases: headless and headful returned
byte-identical responses, so the decision is made before any local lever applies.

### Captcha detection: 6 kinds live

| Target | Result |
|---|---|
| Turnstile, reCAPTCHA v2, hCaptcha | detected + sitekey |
| reCAPTCHA **v3** | detected + sitekey (was misread as v2 with an empty key) |
| GeeTest v4 | detected + `captcha_id` |
| CF interstitial | `None` — correct, it is not a captcha |
| Arkose | **no target** — see below |

### Captcha solving

| Kind | Method | Result |
|---|---|---|
| reCAPTCHA v2 grid | checkbox → local VLM | **3/4, 4/5** (qwen3.8-27b) |
| GeeTest v3 slider | CV gap detection, no model | **3/14 (~20%)** accepted; gap + drag succeed 6/6 |
| hCaptcha grid | shares the reCAPTCHA path | unmeasured |
| image (OCR) | none | no target |

### Model comparison — the model is the whole variable

Identical pipeline across all four rows:

| Model | On disk | Solved |
|---|---|---|
| `google/gemma-4-e4b` | 6.33 GB | 0/5 |
| `qwen/qwen3-vl-8b` | 6.19 GB | 1/5 |
| `qwen/qwen3.6-27b` | 17.48 GB | 5/5 |
| `qwen/qwen3.8-27b` | 17.74 GB | 4/5 |

Both ~6 GB models fail, including one purpose-built for spatial reasoning. The
capability appears around 27B. 4/5 vs 5/5 at n=5 is one dropped attempt, not a
ranking.

### Cookies

| Check | Result |
|---|---|
| Solve → harvest | PASS — token 2212 chars, 1 cookie harvested |
| Clearance survives relaunch | PASS — separate browser starts with `_GRECAPTCHA`; `cookies.sqlite` on disk |
| Whole jar (siblings) travel | PASS (unit) |
| Cross-domain isolation | PASS (unit) |
| Expired clearance not replayed | PASS (unit) |

### yad2 (Radware) — the full cascade on one URL

```
https://www.yad2.co.il/realestate/forsale/center-and-sharon?property=5,39,32,55&area=4&bBox=...&zoom=15
```

| Tier | Result |
|---|---|
| Ladder | 200, 118 KB, `vendor=radware`, `real=False` → escalates |
| Render | 200, **3.3 MB**, `real=True`, **730 listing elements**, 25 prices, 61 cookies |

Both tiers are correct, and getting there required fixing a bug in each
direction — see below.

---

## Reproducing a specific finding

Each of these was a real bug. The commands reproduce the *evidence*, not just the
pass.

**A wall served as HTTP 200.** `dickssportinggoods.com` serves three different
bodies depending on TLS profile alone:

```bash
uv run python -c "
from curl_cffi import requests
from scrapper_tool._challenge import is_interstitial, has_real_content
for p in ['chrome124','chrome','safari18_0']:
    r = requests.get('https://www.dickssportinggoods.com/', impersonate=p, timeout=30)
    print(p, r.status_code, len(r.text), is_interstitial(r.text, r.status_code), has_real_content(r.text, r.status_code))
"
```

**A served page scored as a wall.** The mirror image, and the more expensive one
— it also told the proxy pool to blame a healthy proxy:

```bash
uv run pytest tests/unit/test_challenge.py -k "radware" -v
```

Two fixtures, either of which alone admits a wrong rule: a **118 KB Radware
wall** (marker inside the head window) and a **served page** carrying the same
marker past it. Position, not size, is the discriminator.

**The context-length trap.** Reproduce the false "does not fit":

```bash
lms load qwen/qwen3.8-27b --gpu max           # fails: insufficient resources
lms load qwen/qwen3.8-27b --context-length 8192 --gpu max   # loads in ~11 s
```

---

## Known gaps

Named rather than hidden, because an untested path does not stay working.

- **`aws-waf` and `image` have no target at all.** The `gokuProps` extraction has
  never seen a live page.
- **`arkose`/`funcaptcha` is fixture-verified only.** Measured 2026-08-15: Roblox,
  Microsoft and EA all load with no Arkose script or iframe — the widget is gated
  behind the first form step. Driving a stranger's account-signup form to satisfy
  a detection test is not a trade this project should make.
- **hCaptcha grid solving is unmeasured**, though it shares the reCAPTCHA path.
- **DataDome's slider is unverified live** — DataDome gates on IP before showing
  a puzzle, so no attempt was reachable from this egress.
- **Real proxy egress is unverified.** The handoff is tested; that traffic
  actually leaves via the proxy needs a live proxy and an IP echo.
- **Every number here comes from one datacenter IP.** That is the single largest
  confound, and the one change most likely to move the blocked rows.
