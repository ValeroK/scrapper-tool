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
