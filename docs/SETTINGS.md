# Settings reference

Every knob in `scrapper-tool` is overridable via:

1. **Code** — pass `AgentConfig(...)` or kwargs to `agent_extract` / `agent_browse`.
2. **Environment variable** — `SCRAPPER_TOOL_*` (loaded by `AgentConfig.from_env()`).
3. **`.env` file** — see [`.env.example`](../.env.example). The library does NOT auto-load `.env` — wire it in via `uv run --env-file .env`, `docker compose`, or `python-dotenv`.

Resolution precedence (highest first): explicit kwargs → `config=AgentConfig(...)` → env vars → built-in defaults.

This page is the canonical reference. If a setting isn't documented here, it isn't a public knob.

## Where do I put settings when using as a library?

Pick one of three places. They compose; you can use all three.

| Location | Best for | How |
|----------|----------|-----|
| **OS environment variables** | Deployment, secrets management, 12-factor apps | `export SCRAPPER_TOOL_AGENT_*=...` then call functions normally. |
| **`.env` file + `python-dotenv`** | Local development | `load_dotenv()` BEFORE importing `scrapper_tool`. Or use `uv run --env-file .env ...` / `docker compose` (which loads automatically). |
| **`AgentConfig(...)` Python object** | Tests, multi-tenant apps that vary config per call | `agent_extract(..., config=AgentConfig(model="..."))`. Per-call kwargs override the config. |

`scrapper-tool` itself does **not** auto-load `.env` — that's the calling app's
job, so the library stays predictable. The bundled `docker-compose.yml` does
auto-load `.env` (compose's standard behavior).

### Code examples

```python
# A) Pure env-driven (CI, production, k8s):
import asyncio
from scrapper_tool.agent import agent_extract
result = asyncio.run(agent_extract(url, schema={"type": "object"}))

# B) .env-driven (local dev):
from dotenv import load_dotenv
load_dotenv()                                  # MUST run before imports below
from scrapper_tool.agent import agent_extract  # noqa: E402
# ...

# C) Pure-code (tests, programmatic):
from scrapper_tool.agent import AgentConfig, agent_extract
cfg = AgentConfig(browser="patchright", model="qwen3-coder:30b")
result = await agent_extract(url, schema=..., config=cfg)

# Per-call overrides win over (cfg / env / defaults):
result = await agent_extract(url, schema=..., config=cfg, headful=True)
```

---

## Pattern E — LLM-agent layer (v1.0.0+)

These settings drive `agent_extract`, `agent_browse`, and `agent_session`.

### Browser backend

| Field | Env var | Default | Choices | Notes |
|-------|---------|---------|---------|-------|
| `browser` | `SCRAPPER_TOOL_AGENT_BROWSER` | `camoufox` | `camoufox` / `patchright` / `scrapling` / `obscura` | Camoufox = best stealth, ~200 MB RAM, ~42 s/bypass. Patchright = fast mode, weaker stealth. Obscura = experimental lightweight CDP sidecar (see below). |
| `obscura_cdp_url` | `SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL` | `http://127.0.0.1:9222` | http URL | CDP endpoint for `browser=obscura`. Requires a running `obscura serve --host 0.0.0.0` process (Playwright discovers the ws endpoint from the http URL). |
| `fingerprint` | `SCRAPPER_TOOL_AGENT_FINGERPRINT` | `browserforge` | `browserforge` / `none` | Per-session UA/Accept/Canvas/WebGL randomization. Camoufox ignores this (has its own). |
| `behavior` | `SCRAPPER_TOOL_AGENT_BEHAVIOR` | `humanlike` | `humanlike` / `fast` / `off` | Mouse-path bezier + jittered keystroke timing. Defeats DataDome behavior detection. |
| `headful` | `SCRAPPER_TOOL_AGENT_HEADFUL` | `0` (false) | `0`/`1`/`true`/`false`/`yes`/`no`/`on`/`off` | Show the browser window. Useful for debugging. |
| `proxy` | `SCRAPPER_TOOL_AGENT_PROXY` | unset | URL string | `http://user:pass@host:port` or `socks5://host:port`. Forwarded to the browser. |

#### Camoufox render knobs (v1.6.0+)

Camoufox-native; the other backends ignore them.

| Field | Env var | Default | Notes |
|-------|---------|---------|-------|
| `camoufox_headless_mode` | `SCRAPPER_TOOL_AGENT_CAMOUFOX_DISPLAY` | `headless` | `virtual` runs under an Xvfb virtual display — stealthier than pure headless, and the Docker image ships `xvfb`. Try this first on a target that challenges you in headless. |
| `block_images` | `SCRAPPER_TOOL_AGENT_BLOCK_IMAGES` | `0` | Big speed/bandwidth win when only text/DOM matters. **Camoufox warns this can cause detection issues on major WAFs** — use on unprotected targets, not hard ones. |
| `fingerprint_preset` | `SCRAPPER_TOOL_AGENT_FINGERPRINT_PRESET` | `0` | Use a real bundled fingerprint instead of a generated one (Camoufox 0.5+). |
| `camoufox_os` | `SCRAPPER_TOOL_AGENT_CAMOUFOX_OS` | unset | e.g. `windows`, `macos`, `linux` — match the target's typical audience. |
| `camoufox_locale` | `SCRAPPER_TOOL_AGENT_CAMOUFOX_LOCALE` | unset | e.g. `he-IL`. Matching the site's locale is a real signal on geo-targeted sites. |

#### When to switch backends

| Target | Use |
|--------|-----|
| Cloudflare Enterprise / DataDome / Akamai Bot Manager v4 / Imperva | `camoufox` (default) |
| Lightly-protected SPAs, batch throughput, CI runs | `patchright` |
| You already installed `[hostile]` and don't want another browser | `scrapling` |
| High-volume/parallel batch where RAM per instance is the bottleneck (Linux/Docker) | `obscura` (experimental — run `obscura serve` sidecar, benchmark first) |

### LLM backend

| Field | Env var | Default | Choices |
|-------|---------|---------|---------|
| `llm` | `SCRAPPER_TOOL_AGENT_LLM` | `ollama` | `ollama` / `openai_compat` / `llama_cpp` / `vllm` |
| `model` | `SCRAPPER_TOOL_AGENT_MODEL` | `qwen3-vl:8b` | any tag pulled by your LLM server |
| `captcha_vision_model` | `SCRAPPER_TOOL_CAPTCHA_VISION_MODEL` | unset | model for the captcha image-grid tier when it should differ from `model`; unset reuses `model` |
| `ollama_url` | `SCRAPPER_TOOL_AGENT_OLLAMA_URL` | `http://localhost:11434` | also serves as base URL for `openai_compat` / `llama_cpp` / `vllm` |

#### Recommended models (local, May 2026)

Pick by VRAM. Qwen3-VL is the current open-source SOTA for agentic UI
grounding + screenshot understanding, which is what the browse mode does.

| Model | VRAM target | Strength | Use case |
|-------|-------------|----------|----------|
| `qwen3-vl:8b` | **16 GB** | Best 8B vision-language for web agents; strong tool calling, 256K context | **Default.** Q4_K_M ~6.1 GB; Q8_0 fits in 16 GB for higher OCR fidelity. |
| `qwen3-vl:4b` | **8 GB** | Same family at smaller scale, fits next to browser + vision encoder overhead | Recommended on 8 GB cards / laptops. Q4_K_M ~3.3 GB. |
| `qwen3-vl:2b` | 4-6 GB | Lightweight fallback | Low-end GPUs / iGPUs. |
| `qwen3-vl:30b` | 20+ GB | MoE A3B — top open-source agent quality | When you have the headroom. |
| `qwen3-coder:30b` | 24 GB | Top-tier function calling, text-only | DOM-heavy E2 flows; vision auto-disabled. |
| `deepseek-v3.2` | very large | Best general reasoning + tool use | Heaviest hardware. |

The library auto-detects vision-capable models by name (`vl`, `vision`, `llava`, `minicpm-v`) and disables image input for text-only models to save tokens.

> Vision models carry a fixed ~1.4 GB encoder overhead in addition to the
> quantized weights. The 8 GB / 16 GB targets above account for that plus
> typical browser RAM and a 4-8K KV cache.

### Run budget

| Field | Env var | Default | Notes |
|-------|---------|---------|-------|
| `max_steps` | `SCRAPPER_TOOL_AGENT_MAX_STEPS` | `50` | E2 only. Once exhausted, returns `AgentResult(error="no-match")` (does not raise). |
| `timeout_s` | `SCRAPPER_TOOL_AGENT_TIMEOUT_S` | `120` | Hard ceiling. Exceeded → `AgentTimeoutError`. |

### ToS / safety

| Field | Env var | Default | Notes |
|-------|---------|---------|-------|
| `respect_robots` | `SCRAPPER_TOOL_AGENT_RESPECT_ROBOTS` | `1` (true) | When true, fetch `/robots.txt` and refuse if disallowed. |

---

## CAPTCHA solver cascade

Free OSS by default. Escalates to a paid solver only when an API key is configured.

| Field | Env var | Default | Choices |
|-------|---------|---------|---------|
| `captcha_solver` | `SCRAPPER_TOOL_CAPTCHA_SOLVER` | `auto` | `auto` / `camoufox-auto` / `theyka` / `capsolver` / `nopecha` / `twocaptcha` / `none` |
| `captcha_api_key` | `SCRAPPER_TOOL_CAPTCHA_KEY` | unset | Paid-vendor API key. Triggers Tier-2 escalation. |
| `captcha_paid_fallback` | `SCRAPPER_TOOL_CAPTCHA_PAID_FALLBACK` | `capsolver` | `capsolver` / `nopecha` / `twocaptcha` / `none` |
| `captcha_timeout_s` | `SCRAPPER_TOOL_CAPTCHA_TIMEOUT_S` | `120` | Per-solve cap. |
| `captcha_vision_model` | `SCRAPPER_TOOL_CAPTCHA_VISION_MODEL` | `qwen3.8-27b-apex` | Model for the image-grid tier. Set **empty** to reuse `model`. See below. |

### The grid tier uses a different model from extraction, on purpose

Extraction and captcha grids are different jobs with opposite requirements, so
one model cannot serve both without silently losing at whichever it was not
chosen for. Measured on live reCAPTCHA with its own verify button as ground
truth:

| Model | Grid score | Extraction |
|---|---|---|
| `google/gemma-4-e4b` | 0/5 | fast and accurate — 1.1 s, 3-of-3 fields |
| `qwen3-vl-8b` | 1/5 | good |
| a ~27B VLM | **4-5/5** | a reasoning-distilled model ran 8x slower and *less* accurately |

Hence two settings: `SCRAPPER_TOOL_AGENT_MODEL` for extraction (small, wins on
instruction-following) and `SCRAPPER_TOOL_CAPTCHA_VISION_MODEL` for grids
(large VLM). Since v2.2.2 the vision default is a ~27B rather than inheriting
the extraction model.

**It is safe to leave this pointing at a model this host cannot serve.** The
grid tier is best-effort: it returns an honest `False` and the cascade escalates
past it, so a wrong value costs a diagnostic line rather than a failed scrape.
`scrapper-tool doctor` reports the state in `checks.captcha_vision_model`:

| Value | Meaning |
|---|---|
| `<model> ok` | probed and available |
| `<model> NOT AVAILABLE` | backend is up but does not serve it — a fix line is printed |
| `<model> (LLM unreachable)` | backend down; the `e1` row carries the fix |
| `reuses model (<model>)` | set empty, so extraction's model is used |

Mind the context length: a 27B's 262k default KV cache, not its weights, is what
overflows a 24 GB card.

### `auto` cascade order

| Tier | Solver | Cost | Solves |
|------|--------|------|--------|
| 0 | Camoufox auto-pass | $0 | Most CF Turnstile interstitials |
| 1 | [Theyka/Turnstile-Solver](https://github.com/Theyka/Turnstile-Solver) | $0 | CF Turnstile (managed + invisible) |
| 2 | CapSolver / NopeCHA / 2Captcha | paid | hCaptcha, reCAPTCHA v2/v3, Funcaptcha, GeeTest, AWS WAF, DataDome |

Without a key, Tier 2 is skipped — captcha-encountered → `AgentBlockedError("captcha-encountered")`.

> **Legal/ToS warning.** Solving CAPTCHAs may violate the target site's ToS. Use only on sites you own or have written permission to automate.

---

## Target URL guard (SSRF protection, v2.2.1+)

Every surface that accepts a URL vets it before a request is issued. Private,
loopback, link-local and cloud-metadata targets are refused, as are non-`http(s)`
schemes and hostnames that *resolve* into private space.

**On by default.** A control you have to switch on is not a control. To reach a
legitimate internal target, allowlist it — that keeps the guard running for
everything else — rather than turning the guard off.

| Env var | Default | Purpose |
|---------|---------|---------|
| `SCRAPPER_TOOL_URL_GUARD` | `1` (on) | `0`/`false`/`no`/`off` disables the guard entirely. Logs one loud warning per process. Prefer the allowlist below. |
| `SCRAPPER_TOOL_URL_GUARD_ALLOW` | unset | Comma-separated hostnames, IPs and CIDRs that are permitted anyway, e.g. `127.0.0.1,10.0.0.0/8,fixtures.internal`. The escape hatch for a local fixture server or an authorised internal scrape. |
| `SCRAPPER_TOOL_URL_GUARD_DNS` | `1` (on) | `0` skips the DNS check, so only what is visible in the URL is vetted. For air-gapped or slow-resolver hosts. A hostname pointing into private space will not be caught. |

A refusal is reported, never silent:

- **REST** — `403` with `{"error": "url_not_allowed", "reason", "detail", "remedy"}`.
  Deliberately not `422`, which in this API means "anti-bot blocked" and would
  make a caller escalate to Pattern D against a permanently refused target.
- **MCP** — a normal `200` payload with `error_code: "url_not_allowed"` plus
  `remedy`, and `blocked` left `false` for the same reason.
- **Crawl / map** — a refused *discovered* link is dropped and counted in
  `dropped_by_guard`; the seed is the caller's own URL, so it raises instead.
- **`doctor`** — reports the guard's state in `checks.url_guard`
  (`on` / `on (allowlist: n)` / `on (no DNS)` / `OFF`).

### What it does and does not cover

| Path | Enforcement |
|------|-------------|
| httpx (A/B/C plain, sitemap, robots.txt) | **Per hop.** A redirect into private space is blocked before the connection. |
| curl_cffi ladder | Pre-flight, plus post-flight on the final URL. With `SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS=1` the chain is followed in Python instead and **every hop is vetted before it is issued**. Off by default pending a canary run — see below. |
| render tier (Camoufox / Patchright) | **Page-initiated requests blocked** — `<img>`, `<iframe>`, `fetch()` at a refused host are aborted before they leave the browser. **Navigation redirect hops are not**: Playwright's `route` does not fire for them, so a `302` into private space is still issued and only the post-flight check refuses the body. |
| Pattern D (Scrapling), E1, E2 | Pre-flight on the target only. These tiers own their own navigation. |
| `obscura` subprocess (`batch_fetch`, `obscura_fetch`) | Pre-flight on every URL. An external binary exposes no hook. |

### `SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS`

| Env var | Default | Meaning |
|---------|---------|---------|
| `SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS` | **off** | `1` makes the curl_cffi ladder follow redirects in Python, vetting each hop before issuing it, instead of handing the chain to libcurl. |

The loop reproduces what libcurl gives free — 301/302/303 downgrade a non-GET to
GET and drop the body, 307/308 preserve both, and `Authorization` is stripped on
a cross-origin hop. Cookies are *not* reimplemented: every hop reuses the same
session, so libcurl's own jar keeps applying its domain scoping.

It is off by default for one reason, and it is not timidity: the redirect
semantics are well specified and tested, but what is **not** yet proven is that
issuing the hops ourselves leaves the TLS and header fingerprint byte-identical
to letting libcurl do it. That fingerprint is why Pattern A/B/C exists. Verify
with `scrapper-tool canary` against a redirecting target before enabling it
widely.

**What remains blind.** With the flag off, and on the render/D/E tiers
regardless, a redirect into private space *is issued* — we can only refuse to
return the body. That is not a safe residual: a state-changing GET has already
happened, and the distinct error codes and timings make a serviceable internal
port scanner. It is a known gap, not a closed one.

### `SCRAPPER_TOOL_URL_GUARD_STRICT` — the fully-closed configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `SCRAPPER_TOOL_URL_GUARD_STRICT` | **off** | `1` refuses to *run* any tier that cannot vet its requests before issuing them. |

This is the only setting under which the guard's promise is complete. Everything
in the "remains blind" paragraph above stops being possible, because the tiers
that could do it stop running.

**It costs you capability, and that is the whole trade.** With it on, these are
refused outright:

| Tier | Why it cannot be vetted |
|---|---|
| `d` (Scrapling) | owns its own fetcher, exposes no request hook |
| `render` | page-initiated requests *are* aborted, but Playwright's `route` does not fire for navigation redirect hops |
| `e1` (Crawl4AI) | drives its own browser; no route on its context |
| `e2` (browser-use) | same |
| `obscura` | an external binary; nothing of ours sits between it and the network |
| `ladder` | **only when `..._STRICT_REDIRECTS` is off** — with it on, every hop is vetted and the ladder runs normally |

On a hostile target that means the scrape simply fails: A/B/C is the only tier
left, and it is the one such sites wall. That is the point — containment bought
with reach — and it is a decision for the operator, which is why it is opt-in
rather than a default someone discovers mid-incident.

A refusal raises `UrlNotAllowed` with `reason="uninterceptable_tier"`, so it
surfaces the same way a refused URL does: REST `403`, MCP envelope with
`error_code`. `scrapper-tool doctor` names the refused tiers in
`checks.url_guard` rather than just reporting the flag, e.g.
`on STRICT (tiers refused: d, e1, e2, obscura, render)`.

DNS pinning (resolve once, connect to the pinned address) would close the
remaining resolve-then-connect race and is deliberately **not** done: it breaks
TLS SNI, and with it the impersonation fingerprint that Pattern A/B/C exists to
protect.

## Cascade tiers

`/scrape` (REST) and `auto_scrape` (MCP) run the same ladder:

```
replay cached recipe + fetch/render -> cheapest, often free
A/B/C  curl_cffi TLS impersonation
D      Scrapling (hostile fetcher)
render stealth browser + deterministic extractors   <- NO LLM
E1     Crawl4AI + LLM
E2     browser-use agent            -> priciest, reached automatically (see below)
```

| Env var | Default | Purpose |
|---------|---------|---------|
| `SCRAPPER_TOOL_RENDER_TIER` | `1` (on) | The stealth-render tier. Set `0` to skip straight from D to the LLM tiers. |
| `SCRAPPER_TOOL_RECIPE_CACHE` | `1` (on) | Learn-once / replay. Set `0` to disable both learning and replay. |
| `SCRAPPER_TOOL_RECIPE_DIR` | temp dir | Where learned recipes and domain policies are stored (one JSON file per domain). |
| `SCRAPPER_TOOL_DOMAIN_POLICY` | `1` (on) | Per-domain tier memory (see below). Set `0` to always run the full cascade. |
| `SCRAPPER_TOOL_COOKIE_DIR` | `~/.scrapper-tool/cookies` | Where `scrapper-tool cookies export` writes jars. Created `0700`; each jar is `0600`. |
| `SCRAPPER_TOOL_RENDER_MAX_BACKENDS` | `2` | How many browser backends the render tier may try on one URL before giving up on the tier. A *wall* triggers the retry, and only a wall — see below. |
| `SCRAPPER_TOOL_RENDER_SOLVE_CAPTCHA` | `1` (on) | Let the render tier clear a detected captcha in-page. Costs nothing on a page with no challenge on it. Set `0` to leave captchas to the LLM tiers. |
| `SCRAPPER_TOOL_SKILL_PATH` | bundled | Path to the skill served at `GET /skill` and the `skill://scrapper-tool` MCP resource. Override to vendor house rules on top of the shipped manual. |

### Asking what this deployment can do

`GET /capabilities` reports the valid `browser` names with their engine and CDP
support, which tiers are usable here, and the flags that gate them. `GET /skill`
returns the tool's own operating manual as markdown; the same text is available
over MCP as the `skill://scrapper-tool` resource.

Check `/capabilities` at client startup. It is what turns "E2 is unreachable with
this backend" from a discovery made on the first hostile page into a warning at
boot.

### Reaching E2 automatically

`interactive` is tri-state as of 3.2.0 and defaults to **auto**:

| Value | Behaviour |
|-------|-----------|
| *(unset / null)* | **Auto.** E2 runs once every cheaper tier is exhausted — unless this domain has already failed E2 twice without ever winning, in which case the learned verdict skips it. A single win re-enables the domain permanently, and the policy TTL re-opens even a written-off one. |
| `true` | Force E2 to be reachable, overriding any learned verdict. |
| `false` | Opt out of E2 entirely. This was the default before 3.2.0. |

The old default made the *caller* classify the page, which is the job the cascade
exists to do. Worse, a gated E2 logged as an ordinary `skipped` step, so a client
that never forwarded the flag was indistinguishable from one whose pages did not
need E2 — a real integration ran that way for months without noticing.

A declined tier now logs `reason: "not_permitted"`, which is deliberately
distinct from every other value: it means *we* declined, not that the tier or the
vendor failed. A caller counting failures against a vendor's budget must not
count it.

### Trying more than one browser

A bot wall is a verdict on the *browser*, not on the tier. Measured on one
target, Camoufox got a clean HTTP 200 where Patchright earned a hard WAF block;
on another the reverse held. Backends are complementary rather than ranked, so a
walled render retries on a different **engine** before the cascade pays for an
LLM tier, and the winning backend is remembered per domain.

Only a wall triggers the retry. A timeout, a crash, or a page with no extractable
signal are not backend-dependent verdicts, and retrying them would double the
cost for a guaranteed identical answer.

E2 additionally *filters* backends by capability rather than failing on them:
browser-use attaches over CDP only, and Camoufox is Firefox, so E2 silently runs
on a CDP-capable backend instead of returning the configuration error it used to.
An explicit per-request `browser` always wins.

### Installing `[cookies]` without a Rust toolchain

`pip install 'scrapper-tool[cookies]'` pulls rookiepy, which publishes wheels for
**CPython 3.12 only** — not `abi3`. On 3.13 and 3.14 pip therefore falls back to
the sdist and builds a Rust extension, which needs a toolchain. This project's
own `.python-version` is 3.13, so you will hit it.

You do not need the toolchain. Extraction is a one-shot, host-side step and the
jar it writes is plain JSON, so **the extractor and the cascade do not have to
share an interpreter**. Run the export under 3.12:

```console
$ uv run --python 3.12 --with 'scrapper-tool[cookies]' \
    scrapper-tool cookies export --domain app.example.com
```

That builds a throwaway 3.12 environment, installs rookiepy from its wheel, and
writes the jar to `SCRAPPER_TOOL_COOKIE_DIR` — nothing is left behind and your
main environment is untouched. A persistent venv works identically:

```console
$ uv venv --python 3.12 .venv-cookies
$ uv pip install --python .venv-cookies 'scrapper-tool[cookies]'
```

Then load it from your normal 3.13/3.14 environment:

```python
from scrapper_tool import load_cookies, scrape
result = await scrape(url, cookies=load_cookies("app.example.com"))
```

`browser_cookie3` is a pure-Python alternative that needs no toolchain on any
version, and the backend resolver will use it if it is already importable. It is
**LGPL**, which is why this project never declares it as a dependency —
installing it is your decision, not one the package makes for you.

The `cookies` extra is new in **2.1.0**. On 2.0.0 it does not exist, so
`pip install 'scrapper-tool[cookies]'` there resolves without rookiepy and
`cookies export` reports no backend — pin `scrapper-tool>=2.1.0`.

### The cascade does not remember your login between requests

By design. Cookies you pass in are held for the life of one request and then
dropped — they are never written to the recipe store. That store is keyed by
*domain*, but cookies are per-*identity*, so two callers scraping one domain as
different users would silently share a session. Its TTL is 14 days while a
`cf_clearance` lasts about 30 minutes, and its whole contract is "every read
failure is silent" — fine for a CSS selector, wrong for a credential, where the
worst case of a *successful* read is impersonating the wrong user.

If you want a login to persist across requests, use the mechanism that already
exists for it: export once with `scrapper-tool cookies seed-profile`, then point
`persist_browser_profile_dir` at that directory. It is caller-owned and isolated
per vendor.

The render tier is on by default because it is both cheaper and more reliable
than escalating to an LLM. Measured on real targets: one site returned 403 on
all four TLS profiles yet rendered 1.35 MB of genuine content, and another went
from 4 extractable headlines to 212 once rendered — in both cases the existing
Pattern B/C/CSS extractors then did the job with **zero tokens**. It uses the
browser configured by `SCRAPPER_TOOL_AGENT_BROWSER` and skips itself cleanly
when the `[llm-agent]` extra isn't installed (no entry in `pattern_attempts`).

### Learn-once / replay

When an expensive tier succeeds, the cascade works backwards from the data it
produced to the CSS selectors that would have produced it for free, then caches
that recipe per domain. The next request for that domain replays it — a fetch
plus a selectolax parse instead of a browser launch or an LLM call
(`pattern_used="replay"`).

Three things make it safe to cache a heuristic:

- **Every recipe is verified before it's cached.** The derived schema is run
  through the real CSS extractor and checked against the data it came from. One
  that can't reproduce its own training example is discarded, not stored.
- **The tier it was learned from travels with it.** Selectors for a rendered DOM
  are replayed with a render; only a recipe proven to work against raw HTTP is
  replayed with a plain fetch. When a render-learned recipe *also* matches the
  body A/B/C already fetched, it's downgraded automatically — so a render that
  won for anti-bot reasons rather than JS reasons still yields free replays.
- **Drift self-heals.** A recipe that stops matching is evicted on the spot and
  the normal cascade re-derives a fresh one. A stale recipe costs one wasted
  fetch, once.

Recipes are deliberately *not* derived for JSON-LD/microdata wins: Pattern B
already extracts those deterministically at tier 1, so a CSS recipe would be
strictly more fragile for no gain. Derivation also declines when a page's only
handles are build-generated class hashes and it carries no `data-testid`-style
attribute — a missing recipe just means full price next time, while a wrong one
would mean wrong data indefinitely.

### Per-domain tier memory (self-tuning cascade)

The cascade remembers which tier reached content on each domain and starts there
next time. On a site where the HTTP ladder always 403s and only a render gets
through (store.mopar.com, g2.com), paying for the ladder and Pattern D on every
request just to watch them fail is pure latency; once render has won there twice,
the cascade skips straight to it.

It is deliberately conservative:

- **It only skips *cheaper* tiers, never jumps past a working one.** The worst
  case of a wrong "start at render" is one wasted browser launch — the cascade
  still falls through to the LLM tiers, so it can never produce a wrong answer,
  only a slightly slower one.
- **Two wins at the same tier are required** before a domain is trusted; one
  success could be a fluke (a proxy rotation, a block briefly lifting).
- **It expires after 24h.** A site that tightened *or relaxed* its posture is
  re-discovered — the TTL is the only thing that re-probes the cheap tiers on a
  domain we've learned to skip them on, so without it a site that got easier
  would be stuck on the expensive tier forever.
- The replay tier (cached recipe) always runs first regardless — a cache hit is
  cheaper than every tier the policy chooses between.

Stored under `<recipe dir>/policy/`. A blocked or errored result never teaches
the policy; only a real win does.

### Challenge detection

When the ladder gets a bot-vendor interstitial instead of a page, the response
carries `challenge_detected` — one of `cloudflare`, `radware`, `datadome`,
`perimeterx`, `akamai`, `kasada`, `incapsula`, or `unknown` (null when nothing
was detected). It is reported no matter which tier eventually wins, and it also
steers escalation:

| Detected | Pattern D | Why |
|----------|-----------|-----|
| `cloudflare` | **runs** | Scrapling's `solve_cloudflare` is exactly for this. |
| anything else | **skipped** | Scrapling has no solver for it, so D would burn a browser launch re-fetching the same interstitial. Goes straight to render. |
| nothing | runs | Unchanged behaviour. |

Detection is content-first: a 403 carrying a large real DOM is *not* treated as
a wall (`store.mopar.com` does exactly this), while a bot-walled HTTP 200 is.

### Site-level scraping — `/map` and `/crawl`

`/map` (MCP: `map_site`) discovers URLs: sitemaps declared in robots.txt, then
`/sitemap.xml` as a fallback, plus links from the seed page fetched through the
impersonation ladder. No browser, no LLM. Truncation is always reported via
`truncated` / `dropped_by_limit` — "200 URLs" and "200 of 40,000" are different
answers to plan a crawl on.

`/crawl` (MCP: `crawl_site`) walks a site breadth-first, running the **full auto
cascade on each page**. That means a crawl inherits recipe replay, the render
tier, challenge detection, and proxy rotation for free, and the recipe learned on
page one makes the rest of the crawl cheap. Bounded by `depth`, `max_pages`, and
`concurrency`; the response's `stats` reports `hit_page_limit`, `hit_depth_limit`,
and `queued_but_unvisited` so a bounded crawl is never mistaken for a complete
one. Page HTML is omitted unless you pass `include_html: true` — a 50-page crawl
of rendered pages is tens of megabytes of JSON.

`same_domain` (default true) keeps the crawl on the seed's host **and its
subdomains**, matched at a label boundary against the seed's own hostname. That
deliberately avoids computing a "registrable domain" without a public-suffix
list, which would reduce `yad2.co.il` to `co.il` and treat every Israeli
commercial site as one site.

### robots.txt

`respect_robots` (default true, `SCRAPPER_TOOL_AGENT_RESPECT_ROBOTS`) is now
enforced — previously it was configuration that nothing read. It applies to the
crawler, which is where it matters: a single scrape is a user asking for one page
they could have opened themselves, while a crawler visits pages nobody asked for.

- `Crawl-delay` is honoured, including fractional values. Python's
  `RobotFileParser` parses this directive with `int()` and silently discards
  `Crawl-delay: 0.5`, so it's parsed separately. A delay is capped at 10s of
  actual waiting — honouring a hostile `Crawl-delay: 86400` literally is
  indistinguishable from hanging.
- Status handling follows RFC 9309: 4xx (including the 403 anti-bot systems often
  serve for robots.txt) means no rules published and everything is allowed; 5xx or
  unreachable is treated as a full disallow.
- robots.txt is fetched once per origin per hour, not once per URL.

Set `respect_robots: false` only for sites you own or are authorised to crawl. It
logs a warning when disabled.

### The E2 gate — `interactive`

E2 (browser-use) is the most expensive tier by a wide margin. From v1.6.0 a
blocked E1 no longer auto-escalates into it: pass `interactive: true` (REST) /
`interactive=True` (MCP `auto_scrape`) when the target genuinely needs a
multi-step agent — login, pagination, dynamic forms. Otherwise the cascade stops
at E1 and returns E1's blocked result, which carries the escalation log and
whatever partial content E1 saw.

Rationale: a page that is simply *walled* will wall the agent too, just slower
and for many more tokens. Interaction is the only thing E2 can do that the
render tier can't. An explicit REST `mode="browse"` is a direct request for E2
and is never gated.

## Install extras

**Recommended SDK install** for all capabilities:

```bash
uv pip install scrapper-tool[full,agent]
```

The default Docker image (`Dockerfile` / `docker compose build scrapper-tool`)
is also the full one — every pattern wired up.

| Extra | What it adds | Mutually exclusive with |
|-------|--------------|-------------------------|
| (none) | Patterns A/B/C — `httpx` + `curl_cffi` + `selectolax` + `extruct`. | — |
| `[agent]` | The MCP server (`scrapper-tool-mcp`). Compatible with everything. | — |
| `[hostile]` | Pattern D — Scrapling + Playwright. Pins `lxml>=6`. | `[llm-agent]` (when installed via plain pip) |
| `[llm-agent]` | Pattern E — Camoufox, Patchright, browser-use, Crawl4AI, Browserforge, langchain-ollama, Pillow. Pins `lxml~=5.3`. | `[hostile]` (when installed via plain pip) |
| `[turnstile-solver]` | Captcha cascade Tier 1 (Theyka). Compatible with `[llm-agent]`. | — |
| **`[full]`** ⭐ | All five patterns: A/B/C/D/E in one environment via uv's `override-dependencies` declaration. | — (uv-only) |
| `[skyvern-backend]` | Reserved for a future Skyvern E2 backend. | — |

The `obscura` browser needs no extra — it reuses the Playwright client from
`[llm-agent]` and connects to an external `obscura serve` process. The removed
`[zendriver-backend]` / `[botasaurus-backend]` extras (v1.5.0) are no longer
available; use `obscura` for a lightweight CDP-driven backend.

`[full]` is a uv-only install path — it relies on `[tool.uv] override-dependencies`
in `pyproject.toml` to coerce both Scrapling and Crawl4AI onto a single
`lxml>=6.0.3`. Plain `pip` doesn't honor that override; with pip, install
`[hostile]` and `[llm-agent]` in separate environments OR pass
`--constraint` with `lxml>=6.0.3` and accept the resolver warning.

## Live test toggles

| Env var | Default | Purpose |
|---------|---------|---------|
| `SCRAPPER_TOOL_LIVE` | unset | Set to `1` to enable Pattern A/B/C live probes (`tests/integration/test_live_probes.py`). |
| `SCRAPPER_TOOL_AGENT` | unset | Set to `1` (with `SCRAPPER_TOOL_LIVE=1`) to enable Pattern E live probes (`tests/integration/test_agent_live.py`). |

---

## Settings NOT covered by env vars

A few power-user knobs are code-only because they don't fit a flat env-var shape:

| Knob | Set via | Purpose |
|------|---------|---------|
| `instruction` (E1) | `agent_extract(..., instruction="...")` | Free-form extraction guidance. |
| `schema` (E1/E2) | function arg | Pydantic class, JSON Schema dict, or natural-language string. |
| `BehaviorPolicy` constructor knobs | `HumanlikePolicy(keystroke_median_ms=..., …)` | Fine-tune timing distributions. |
| `BrowserforgeGenerator(browser=..., os_family=...)` | constructor | Override fingerprint distribution. |

---

## Examples

### Override a single value per call

```python
from scrapper_tool.agent import agent_extract

# Overrides go via **kwargs and merge with env / defaults.
result = await agent_extract(
    "https://example.com",
    schema={"type": "object"},
    model="qwen3-coder:30b",     # override default model
    browser="patchright",         # override default backend
    timeout_s=240,
)
```

### Build a config once, reuse for many calls

```python
from scrapper_tool.agent import AgentConfig, agent_session

cfg = AgentConfig(
    browser="camoufox",
    model="qwen3-vl:8b",
    behavior="humanlike",
    captcha_solver="auto",
    timeout_s=180,
)
async with agent_session(config=cfg) as s:
    a = await s.extract("https://a.example", schema=...)
    b = await s.browse("https://b.example", "log in and ...")
```

### Read everything from env (deployment-friendly)

```python
from scrapper_tool.agent import AgentConfig

cfg = AgentConfig.from_env()   # reads all SCRAPPER_TOOL_* vars
```

---

## HTTP REST sidecar (scrapper-tool-serve)

Available since v1.1.0. See [`http-sidecar.md`](http-sidecar.md) for the endpoint reference. These env vars configure the FastAPI server itself — agent / browser / captcha settings (above) are forwarded automatically.

| Field | Env var | Default | Notes |
|-------|---------|---------|-------|
| `host` | `SCRAPPER_TOOL_HTTP_HOST` | `0.0.0.0` | Bind address. Use `127.0.0.1` to restrict to localhost. |
| `port` | `SCRAPPER_TOOL_HTTP_PORT` | `5792` | TCP port. Avoids the crowded 8000/8080 range. |
| `api_key` | `SCRAPPER_TOOL_HTTP_API_KEY` | (unset) | When set, all `/fetch /scrape /extract /browse` requests must include `X-API-Key: <value>`. Leave unset for internal-only sidecar networks. |
| `cors_origins` | `SCRAPPER_TOOL_HTTP_CORS_ORIGINS` | `*` | Comma-separated CORS allowed origins. Use explicit origins (`https://app.example.com`) in production. **`*` disables credentialed CORS** — see below. |
| — | `SCRAPPER_TOOL_HTTP_ALLOW_UNAUTH_COOKIES` | (unset) | Allow `POST /scrape` to accept a `cookies` body when no API key is configured. Localhost development only — see below. |
| `log_level` | `SCRAPPER_TOOL_HTTP_LOG_LEVEL` | `info` | Uvicorn log level. One of: `debug` / `info` / `warning` / `error` / `critical`. |
| `serve_docs` | `SCRAPPER_TOOL_HTTP_DOCS` | `1` | When `0`, `/docs` and `/redoc` are not served. `/openapi.json` always works. Disable in production for reduced attack surface. |

### Cookies and the sidecar

Two settings above interact with credentialed requests, and both default to the
safe side rather than the convenient one.

**Wildcard CORS disables credentials.** The CORS spec forbids
`Access-Control-Allow-Origin: *` together with
`Access-Control-Allow-Credentials: true`, and the assumption that browsers make
the pairing inert does not hold here: against the pinned Starlette, wildcard
origins cause the request's `Origin` to be *reflected* verbatim while
`allow-credentials: true` is still sent. Any page on any origin could then make
credentialed cross-origin requests to the sidecar and read the responses. So
when origins are wildcarded, credentialed CORS is switched off. Nothing
legitimate is lost — the spec requires enumerating origins for credentials
anyway. List them explicitly if you need it.

**Cookies on an unauthenticated sidecar are refused.** `SCRAPPER_TOOL_HTTP_API_KEY`
is unset by default, so `/scrape` is open out of the box. That is defensible for
anonymous scraping and indefensible once a request body carries a live session
cookie: anyone who can reach the port could replay someone's session through
this host's egress IP. A `cookies` body without an API key therefore returns
`403`. Only requests that actually carry cookies are affected.

`SCRAPPER_TOOL_HTTP_ALLOW_UNAUTH_COOKIES=1` disables that check. It exists for
localhost development. Do not set it on anything reachable from another machine
— set an API key instead.
