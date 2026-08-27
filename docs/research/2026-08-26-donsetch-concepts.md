# DonSeTch concept audit — 2026-08-26

> **Source read**: [dondai44423/donsetch](https://github.com/dondai44423/donsetch) at `master`, ~51k LOC Rust, read from source (shallow clone, nothing executed). **Refresh trigger**: a v4 release, or a concept below moving from `candidate` to `adopt`.

Companion to [`2026-04-30-landscape.md`](2026-04-30-landscape.md). That doc surveyed *libraries we could depend on*; this one surveys *a competitor's architecture for ideas we can rebuild*. Same output format: every row ends in adopt / candidate / reject, with the reason.

**Licensing constraint, up front.** DonSeTch is **AGPL-3.0**. `scrapper-tool` is MIT. No code, no comment blocks, no constant tables may be copied across — an AGPL fragment would relicense our distribution. Everything below is a *concept* to be reimplemented from the description. Where a specific constant is quoted (BM25 `k1=1.2`, a 3x latency multiplier) it is quoted as a published parameter choice to calibrate against, not as text to lift.

---

## §1 — Why this is not an apples-to-apples comparison

| | **DonSeTch** | **scrapper-tool** |
|---|---|---|
| Shape | One binary; MCP server + CLI | Python library; MCP server + REST sidecar |
| Surface | 3 tools (`web_fetch`, `web_search`, `web_crawl`) | 8 MCP tools, 10 REST endpoints, 5 patterns |
| Output | Clean markdown for an agent to read | Validated structured data against a caller schema |
| Consumer | An LLM doing web research | An adapter extracting one vendor's fields |
| Dependencies | Zero OSS web tooling — own HTTP/1.1 + HTTP/2, own HPACK, vendored BoringSSL, PDFium FFI | `httpx`, `curl_cffi`, `selectolax`, `extruct`, Scrapling, Crawl4AI |

The divergence matters for reading the rest of this doc. **Their entire token-economy layer transfers directly to our MCP surface** — we serve the same consumer there and we serve it worse. **Their transport, search, and PDF stacks do not transfer at all** — `curl_cffi` plus the impersonation ladder is our answer on TLS by a decision already argued in §1 of the landscape doc, and search aggregation and PDF parsing are outside this lib's scope.

---

## §2 — Concept inventory

Status column: **have** (we do this), **partial** (we do part of it), **gap** (we don't).

### §2.1 Token economy — the agent's context as the scarce resource

This is the theme where they are furthest ahead of us, and the theme most directly applicable to our MCP server.

| Concept | Their mechanism | Us | Status |
|---|---|---|---|
| **Block-model markdown** | `src/extract/blocks.rs` — typed blocks (Heading/Para/List/Table/Code/Quote/Media) with heading breadcrumbs; links stripped by default, link-farm lists and bare-link lines dropped, cross-block dedup | `mcp.py` returns **raw HTML truncated at 64 KB** (`_BODY_TRUNCATION_BYTES`). That is roughly 16-20k tokens of mostly markup for one page | **gap** |
| **`focus=` block filter** | `src/extract/focus.rs` — hand-rolled BM25 (`k1=1.2`, `b=0.75`) over blocks, 12-language tokenizer with CJK bigrams, stopwords, light stemming, accent folding. **No hits returns the full page** — a bad query never punishes the agent | nothing | **gap** |
| **`toc` + `section`** | Heading outline with section ids and sizes as one cheap call, then fetch one section | nothing | **gap** |
| **Pagination** | `next_offset` in the response; call again with `offset` | we set a `truncated: true` bool and provide **no way to get the rest**. Strictly worse than no pagination: the agent is told data is missing and cannot retrieve it | **gap** |
| **Reference handles** | `src/handles.rs` — `L{n}` link handles interned per URL (stable, LRU 2048, TTL 24h, atomic tmp+rename persistence) and `S{n}` position-bound search handles; `fetch L12` works. Raw URLs stay in `structuredContent` for citation. Claimed 80 tokens per URL down to 3 | nothing | **gap** |
| **Probe mode** | `must_contain="X"` verifies a claim against the fully-fetched page but returns MATCH/NO-MATCH plus at most 3 excerpts | nothing | **gap** |
| **Page memory** | `src/pages/history.rs` — sha256-12 fingerprint of normalized markdown per URL; a re-fetch reports unchanged / changed / rewritten with section-level diffs; `since_last=true` collapses a re-check to one line. Bounded: 64 KB text per URL, 4 MB total, 512 URLs, oldest-first eviction | nothing | **gap** |

The load-bearing insight is not any single parameter — it is that **the response is a budget and every field has to earn its tokens**. Their `bench/tokens.py` turns that into an assertion (see §2.5).

### §2.2 Contract discipline

| Concept | Their mechanism | Us | Status |
|---|---|---|---|
| **One spec, many frontends** | `src/spec.rs` is the single source of truth: `mcp_schema()` builds the MCP `tools/list` JSON schema, `cli_command()` builds the clap subcommand, `matches_to_json()` converts argv back into the exact JSON the MCP dispatcher receives. One `help` string serves as both the schema description and the CLI help. Stated maintenance rule: a parameter changes **here, once** | Our two surfaces each declare their own parameters and **they have already diverged**. REST `ScrapeRequest` has `mode`, `cookies`, `headful`, `force_llm_extract`, `solve_cloudflare`; MCP `auto_scrape` has `hostile_only` and none of those five. Same cascade underneath. The `_classify.py` header documents a previous instance of exactly this drift | **gap** |
| **Stable machine error codes** | Every error carries a code from a fixed vocabulary — `wall.challenge`, `wall.paywall`, `guard.ssrf`, `deadline.hit`, `content.binary`, `archive.stale`. Documented contract: branch on codes, not prose | `errors.py` is a genuinely good exception hierarchy **for Python callers**, with circuit-breaker semantics baked in. But the MCP/REST envelope (`_agent_error_payload`) carries a prose `error` string and a `blocked` bool — an agent cannot branch on it | **partial** |
| **`next_action` on every error** | The error payload names the remedy: *"retry with a narrow selector= or focus=; if the page is JS-heavy, tier=2 renders it in a browser"*, *"private/loopback targets are blocked by design — use a public URL"*. Folded into the text body too, because some clients drop `structuredContent` | `doctor.py` already does exactly this for **install** health (per-tier status plus the exact fix command). The pattern is in our codebase; it just never reached runtime errors | **partial** |
| **Honest failure** | Never claim success on a wall (their WRB run reports 0 false positives); a `degraded:` footer names engines that failed; typed stop reasons (`FrontierEmpty` / `MaxPages` / `CharBudget` / `DepthLimit` / `Deadline` / `ThrottledOut`) distinguish "done" from "use resume" | We are **already strong here** and it is a stated value of the repo: `crawl` reports what its bounds left unvisited, `map` reports truncation counts, `challenge_detected` surfaces the vendor that walled us, `skipped_reason` is per page, and the v2.2.0 docs publish the numbers that did not work. Missing only the typed stop-reason enum | **have** |
| **Time control** | `deadline_ms` on every call (500-600000) with an honest deadline error rather than a hang, an `ms` cost footer on every result, MCP progress notifications per crawl page, real cancellation | `timeout_s` per call. No cost reporting, and a 25-page `crawl_site` is silent for minutes | **partial** |

### §2.3 Adaptive behaviour

| Concept | Their mechanism | Us | Status |
|---|---|---|---|
| **Crawl Governor** | `src/crawl/governor.rs`. Four ideas worth separating: (1) pacing is per **(host, lane)** so two proxies to one host are two independent rate clocks; (2) the penalty box is shared **per host across lanes**, so a 429 on one lane backs all of them off; (3) **rising EWMA latency is a leading indicator** — above 3x the first-observed baseline it adds a backoff rung *before* the wall lands; (4) deterministic jitter in [0.75, 1.25] because fixed intervals are themselves the fingerprint. robots `Crawl-delay` acts as a floor, not a replacement | fixed `concurrency` cap plus robots `Crawl-delay` | **gap** |
| **Self-improving route** | `src/ghost/cache.rs` — `route_for(host)` returns Cold / Warm / SkipToSolve / RecheckCold from a persisted per-domain profile | **`recipe/policy.py` is the same idea, independently arrived at**, with the same guardrails: only ever skips *cheaper* tiers, requires `_MIN_OBSERVATIONS=2` before trusting a domain, expires on TTL so a site that relaxed gets re-probed | **have** |
| **Adaptive cookie TTL** | Where they go further: `observed_lifetime = min(previous, now - last_solved)` converges on a domain's real clearance-cookie lifetime, so a stale-warm fetch is skipped rather than attempted. `warm_fail_streak` requires two consecutive failures before declaring stale (one is usually challenge rotation) | our v2.2.0 clearance-cookie reuse has no learned lifetime and no streak damping | **gap** |
| **`replay_ok` gate** | Warm routing is only offered after a tier-1 replay of ghost-harvested cookies came back **verified good**. Cookies a vendor binds to the browser fingerprint therefore never earn a doomed tier-1 round-trip | we reuse clearance cookies without ever verifying that the cheap tier can actually use them | **gap** |
| **Solve-and-bounce** | The browser exists to produce cookies or rendered HTML, never to fetch content; cookies go to tier 1, the browser is SIGSTOP'd after 20s idle and reaped after 10 min frozen | same direction, arrived at in v2.2.0 (clearance cookies survive to the next tier and, via a persisted profile, the next run). We hold no frozen-process lifecycle | **partial** |
| **Frontier canonicalization** | `src/crawl/frontier.rs` strips roughly 40 tracking params (`utm_*`, `fbclid`, `gclid`, `msclkid`, `mc_cid`, `_ga`, ...) before dedup, on the stated grounds that `?utm_source=` copies of every page kill token budgets | `normalise_url` (`crawl/map.py:115`) strips **fragments only**. Every tracking-param variant of one page is a distinct URL and gets crawled separately | **gap** |
| **Best-first frontier** | `src/crawl/score.rs` — BM25-lite over anchor text (weight 3.0) and URL path tokens (weight 1.5), normalized by query length, plus a depth prior. Budget goes to pages that match the topic instead of to sitemap order | breadth-first, unranked. BFS is the right *default* and the docstring argues that well, but there is no way to say "spend these 25 pages on pricing" | **gap** |
| **Near-dup + resume** | title plus first 200 normalized chars hashed for near-dup skipping; resume tokens valid 30 min let a budget-stopped crawl continue | exact-URL `seen` set only; no resume | **gap** |

### §2.4 Structure and safety

| Concept | Their mechanism | Us | Status |
|---|---|---|---|
| **URL-rewrite adapters** | `src/adapters/mod.rs` has two hooks, and the **rewrite** hook is the interesting one: some pages have a better URL — the site's own public JSON endpoint (Reddit `.json`, the npm/PyPI/crates.io/Go/RubyGems registries). Rewriting gets structured truth in one cheap tier-1 request **and often skips the wall entirely**, because registry CDNs do not challenge | our recipe store and patterns cover the *extract* half well. We have no rewrite hook — no notion that the cheapest anti-bot tier is the one you never trigger | **gap** |
| **Adapter discipline** | Every adapter returns `None` on anything it does not confidently recognize; the generic pipeline is always the fallback, so a site redesign degrades one adapter and never the core. Output is labelled `via=adapter:...` so the agent knows. `DONSETCH_NO_ADAPTERS=1` kill switch; `DONSETCH_ADAPTER_DUMP=<dir>` writes every inspected body for fixture capture | our fallback discipline is equivalent (a recipe miss falls through to the full cascade). We have no kill-switch env and no dump-to-fixture switch — our fixtures are hand-captured | **partial** |
| **SSRF guards** | `src/fetch/guards.rs` runs **before any network call**: private/loopback/link-local literals and localhost names rejected, plus a post-resolution IP check to close DNS rebinding. Error code `guard.ssrf`, and `next_action` explains the block | **nothing** — no URL host is ever checked on any surface. Compounding it: the sidecar's API key is *optional* (`_check_api_key` is a no-op when `SCRAPPER_TOOL_HTTP_API_KEY` is unset), `docker-compose.yml` publishes `${SCRAPPER_TOOL_HTTP_PORT:-5792}` on **all** interfaces (contrast the CDP entry, deliberately bound `127.0.0.1:9222`), and the compose comment recommends leaving the key unset "for internal-only sidecar". Anyone who reaches it can have it fetch `169.254.169.254` or any RFC1918 host and read the body back | **gap** |
| **Anti-cloak check** | On domains known to serve decoys to plain HTTP, the tier-1 response is equivalence-checked against a headless render and stamped `decoy_suspected` rather than passed off as content | `looks_unhydrated` in `_challenge.py` is the same instinct and was found the same way (one.co.il: 200, 419 KB, JSON-LD present, headings still `{displayTitle}`). Ours is a heuristic on one response; theirs is a cross-tier comparison | **partial** |
| **Archive fallback** | `archive=auto` serves the nearest Wayback snapshot for a dead link, labelled `ARCHIVED COPY — 2021-04-03 (5 years old)` | nothing | **gap** |

### §2.5 Engineering discipline

| Concept | Their mechanism | Us | Status |
|---|---|---|---|
| **Fuzz every parser** | `fuzz/` — 5 cargo-fuzz targets (extract, charset, paginate, sitemap, feed). Rationale stated plainly: *a daemon abort is the worst failure mode for an MCP server — one panic kills the session*. CI runs a 90s smoke of every target per push; a found crash becomes a corpus seed so it cannot regress | none. Our parsers eat hostile input by definition — sitemap XML from an untrusted host, charset-ambiguous bodies, the challenge-signature scan, `extruct`/`selectolax` on adversarial markup — and our MCP server has the same one-panic-kills-the-session property | **gap** |
| **Benchmarks that fail** | `bench/tokens.py` does not report numbers, it **asserts invariants**: focus must cut at least 40% on a long structured page, `toc` must cost under 3% of the full page, probe output must stay under 400 chars, handles must beat 60 chars per link. A regression is a build failure. Same discipline on their h2 preface, asserted byte-identical to Chromium in CI | our empirical culture is good — `canary_targets.yaml`, live integration probes, and `docs/research/2026-07-live-validation.md` publishes measured failures honestly. But nothing is a **budget with teeth** | **partial** |
| **Crash-only daemon** | `donsetch mcp --supervised` — a panic restarts the daemon, state reloads from disk, the session survives. SIGKILL-verified | n/a for the library; relevant for the sidecar and MCP server | **candidate** |
| **Bench-before-claim** | Their search benchmark ships a methodology section that argues **against its own headline number** (answer-in-snippet is an easier bar than Tavily's LLM-graded metric; 110 hand-curated questions vs SimpleQA's 4,326) | this is already our house style — see the v2.2.0 note that 2.1.0's anti-bot code "had never met a live host" | **have** |

---

## §3 — Where we are ahead

Stated plainly, because the token-economy gap above is large and it would be easy to read this doc as one-sided.

| | Us | Them |
|---|---|---|
| **CAPTCHAs** | Five-tier cascade, cheapest first: settle, click, slider by pure geometry (no model), local VLM on image grids, paid solver. Measured live: reCAPTCHA v2 grids 3/4-4/5, GeeTest sliders around 20% with no model at all. And we document that v3 and AWS WAF are *not* solvable because they are risk scores, not puzzles | Refuses by design. hCaptcha / reCAPTCHA / Turnstile is an "honest dead end". No counterpart to the geometric slider solver at all |
| **Structured extraction** | Schema-driven: the caller supplies a JSON Schema, a list of fields, or natural language, and gets validated data back. Patterns A/B/C plus `extruct` plus microdata plus recipe replay is a much deeper stack on this axis | Markdown out. Structure is whatever the block model produced |
| **Test determinism** | Unit suite runs with **no live HTTP** — `FakeCurlSession`, `replay_fixture`, golden snapshots | 637 tests plus fuzz, but the search and fetch benches need live network and rotating proxies; their own README concedes free proxies die fast |
| **Install diagnosis** | `doctor` reports per-tier status with the exact fix command, exit codes 0/1/2, and `--require-tier` as a CI or container healthcheck gate. It distinguishes a *module that imports* from a *binary that is missing* — a real failure the published image once shipped | `doctor` exists and is thinner |
| **robots.txt correctness** | RFC 9309 status-code semantics (4xx means no rules published, 5xx means full disallow, because a struggling server is the one case where guessing "allow" is unforgivable), `Crawl-delay` honoured, non-blocking fetch so a concurrent crawl does not stall the loop | robots respected for crawl; `fetch` does not check it |
| **Library-consumer contract** | Circuit-breaker semantics are part of the exception taxonomy: `ParseError` is our bug, 5xx/429/transport is theirs, `BlockedError` must *not* trip a breaker. Plus a generic Adapter Protocol | Not a library; no such contract to get right |
| **Credential handling** | Caller cookies threaded to every tier that can carry them, write-only — never echoed in a response, never logged — and refused with 403 if the sidecar is unauthenticated | No authenticated-session support; explicitly out of scope |

---

## §4 — Adoption list, ranked

Ordered by payoff over effort, not by how interesting the idea is.

| # | Concept | Why now | Effort | Verdict |
|---|---|---|---|---|
| 1 | **SSRF guard + sidecar default-deny** | The only item here that is a live security hole rather than a missed optimisation. Pre-network host check and post-resolution IP check, a `guard.ssrf` code, and flip the API key from opt-in to required-unless-explicitly-disabled | S | **adopt now** |
| 2 | **Tracking-param canonicalization** | One constant tuple and two lines in `normalise_url`. Immediately stops crawl budget going to `?utm_source=` duplicates of pages we already have | S | **adopt now** |
| 3 | **Error codes + `next_action` in the envelope** | Turns every wall into an actionable instruction instead of a prose string. `doctor.py` already proves we write good remedies; extend the vocabulary to runtime and add a stable code per case | S | **adopt** |
| 4 | **`deadline_ms` + `elapsed_ms`** | A hard per-call budget with an honest deadline error, and cost reported on every result. Our `timeout_s` covers half of this; the reporting half is what lets a caller tune | S | **adopt** |
| 5 | **`replay_ok` gate + learned cookie TTL** | Directly hardens v2.2.0's newest and least-proven feature. Verify a harvested cookie actually works at the cheap tier before routing there; learn each domain's real clearance lifetime; require two consecutive failures before declaring stale. Fits inside `recipe/policy.py` and `cookies.py` as they stand | S-M | **adopt** |
| 6 | **Token economy for the MCP surface** | The biggest single win and the biggest effort. Phase it: (a) replace the 64 KB raw-HTML truncation with a block-model markdown render, (b) add `focus=` BM25 block filtering, (c) add `toc`/`section`, (d) add `offset`/`next_offset` so `truncated` stops being a dead end. Step (a) alone probably cuts per-page context by an order of magnitude | L | **adopt, phased** |
| 7 | **Single spec table for all surfaces** | We have *documented* divergence between MCP and REST, and five parameters currently missing from one side. One table generating the Pydantic model, the MCP schema, and eventually a CLI stops the drift structurally instead of by review | M | **adopt** |
| 8 | **Governor-style pacing** | The EWMA-latency-as-leading-indicator idea is the genuinely novel piece and is worth having independent of the rest: it backs off *before* the penalty box, not after. Per-(host, lane) clocks matter as soon as proxy rotation is in play, which it already is | M | **adopt** |
| 9 | **Page fingerprint + delta crawl** | Arguably a *bigger* win for us than for them. Our consumer is a price monitor: "has this page changed since last run" is the whole question, and a delta crawl that skips unchanged pages saves the expensive tiers, not just tokens | M | **adopt** |
| 10 | **Resume tokens + typed stop reasons** | We already report what bounds left unvisited — this makes that report actionable. Typed enum first (cheap), resume token second | M | **adopt** |
| 11 | **URL-rewrite adapters** | The cheapest anti-bot tier is the one never triggered. Worth building for whichever of our vendors exposes its own JSON endpoint, with the kill-switch env and the fixture-dump env from day one — the dump switch would have saved capture work on the fixtures we already have | M | **adopt** |
| 12 | **Fuzz the parsers + assert-based budgets** | `hypothesis` for property tests, `atheris` for true fuzzing; sitemap XML, charset decode, and the challenge scanner first. Then convert the numbers in `docs/research/` into asserted budgets so a regression fails CI instead of being noticed six months later | M | **adopt** |
| 13 | **Best-first focus-ranked frontier** | Real value, but BFS is the right default for "crawl this vendor" and the docstring defends that well. This is an *option*, not a replacement | M | **candidate** |
| 14 | **Reference handles / probe mode** | Elegant, and clearly right for a research agent following links. Less obviously right for our consumer, which usually arrives already knowing the URL it wants. Revisit if `crawl_site` output starts driving follow-up fetches | M | **candidate** |
| 15 | **Crash-only supervised daemon** | Relevant to the sidecar and MCP server, not the library. Worth it once the MCP server is something people leave running | M | **candidate** |
| 16 | **Archive fallback** | For our use case stale data is worse than absent data — a five-year-old price presented as a price is a bug, not a fallback | S | **reject** |
| 17 | **Own TLS/h2 stack, keyless search, PDF pixel-fusion, ONNX reranker** | Out of scope, and the TLS question is already settled: `curl_cffi` plus the ladder buys most of the benefit at a fraction of the maintenance. Their approach is defensible *because* they were willing to write 51k lines of Rust to get it | XL | **reject** |

---

## §5 — Caveats on their published claims

Everything in §2 is read from source and is verifiable. Their **numbers** are not, and should be treated as directional:

- **Search quality (95.5%)** is self-designed and self-run. To their credit the README argues against its own headline: the metric is answer-in-snippet, not LLM-graded, on 110 hand-curated questions rather than SimpleQA's 4,326. That is a lower bar than Tavily's 93.3%, so the two numbers are not comparable, and they say so.
- **TLS parity (JA4, Akamai h2 fingerprint).** The *architecture* is verified in source, not just claimed: `boring`, `boring-sys`, and `tokio-boring` are direct dependencies, `src/transport/tls.rs` drives BoringSSL through raw FFI to switch on the Chrome-specific extensions (OCSP stapling, signed cert timestamps, brotli cert compression, ALPS via `SSL_add_application_settings`), and `src/fetch/client.rs` holds the `SslConnector` on the page-fetch path. The "emergent from the real engine, not a fingerprint table" framing is therefore a genuine structural difference from `curl-impersonate`, which is what our `curl_cffi` sits on. What remains unverified by us is the *byte-identical parity* claim — that is asserted in their CI against live Chromium, and we have not reproduced it.
- **"No `reqwest`. No `hyper`."** is marketing shorthand rather than literally true: `reqwest` is a direct dependency. Every use is auxiliary, though — the BYOK search providers (Exa, Serper, Tavily, TinyFish) and the OCR/reranker model downloads. There are no `reqwest` calls on the page-fetch path, so the substantive claim holds.
- **CAPTCHAs are not solved, and the README says so three times** — do not read the "bypass bot walls" tagline as captcha solving. Verified in source: there is no solver dependency in `Cargo.toml` and every captcha reference in `src/` sits in `detect/walls.rs`, i.e. detection so the tool can report an honest block. See §3 — this is an axis where we are ahead, not behind.
- **WRB fetch/search/crawl scores** come from a benchmark written by the same author as the tool. The honesty metric it introduces — does the tool claim success when it actually got a bot wall? — is a good idea worth borrowing regardless of the score.
- **The vs-wigolo and vs-Hound tables** are marketing artifacts. Cold-start timings and success rates against named competitors are not reproducible from what is published.

None of this weakens the concepts. It just means we calibrate our own numbers ourselves, which is already the house rule.
