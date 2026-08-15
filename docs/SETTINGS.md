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

### `auto` cascade order

| Tier | Solver | Cost | Solves |
|------|--------|------|--------|
| 0 | Camoufox auto-pass | $0 | Most CF Turnstile interstitials |
| 1 | [Theyka/Turnstile-Solver](https://github.com/Theyka/Turnstile-Solver) | $0 | CF Turnstile (managed + invisible) |
| 2 | CapSolver / NopeCHA / 2Captcha | paid | hCaptcha, reCAPTCHA v2/v3, Funcaptcha, GeeTest, AWS WAF, DataDome |

Without a key, Tier 2 is skipped — captcha-encountered → `AgentBlockedError("captcha-encountered")`.

> **Legal/ToS warning.** Solving CAPTCHAs may violate the target site's ToS. Use only on sites you own or have written permission to automate.

---

## Cascade tiers

`/scrape` (REST) and `auto_scrape` (MCP) run the same ladder:

```
replay cached recipe + fetch/render -> cheapest, often free
A/B/C  curl_cffi TLS impersonation
D      Scrapling (hostile fetcher)
render stealth browser + deterministic extractors   <- NO LLM
E1     Crawl4AI + LLM
E2     browser-use agent            -> priciest, interactive=true only
```

| Env var | Default | Purpose |
|---------|---------|---------|
| `SCRAPPER_TOOL_RENDER_TIER` | `1` (on) | The stealth-render tier. Set `0` to skip straight from D to the LLM tiers. |
| `SCRAPPER_TOOL_RECIPE_CACHE` | `1` (on) | Learn-once / replay. Set `0` to disable both learning and replay. |
| `SCRAPPER_TOOL_RECIPE_DIR` | temp dir | Where learned recipes and domain policies are stored (one JSON file per domain). |
| `SCRAPPER_TOOL_DOMAIN_POLICY` | `1` (on) | Per-domain tier memory (see below). Set `0` to always run the full cascade. |
| `SCRAPPER_TOOL_COOKIE_DIR` | `~/.scrapper-tool/cookies` | Where `scrapper-tool cookies export` writes jars. Created `0700`; each jar is `0600`. |

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
