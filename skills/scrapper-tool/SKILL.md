---
name: scrapper-tool
description: >-
  Use when the task is to get data off a website — scrape a page, extract a
  product/price, pull structured fields, discover a site's URLs, or crawl a
  whole site — especially when the site is protected by anti-bot systems
  (Cloudflare, DataDome, Akamai, PerimeterX) or renders its content with
  JavaScript. scrapper-tool is a Python toolkit with an auto-escalating cascade
  that tries the cheapest method first and climbs to a stealth browser or a
  local LLM only when the site forces it. Also solves captchas encountered along
  the way (Turnstile, reCAPTCHA v2, hCaptcha, GeeTest/DataDome sliders) and keeps
  the clearance it wins. Reach for this instead of a bare `requests`/`fetch` when
  a plain GET would be blocked or would return an empty JS shell.
---

# scrapper-tool

scrapper-tool turns "get the data from this URL" into one call. Behind that call
is a **cascade** of extraction tiers, cheapest first; it stops at the first tier
that reaches real content, learns a cheap recipe from any expensive win, and
remembers per-domain which tier worked so the next request is faster.

You do **not** pick a tier. You call one entrypoint and read `pattern_used` from
the result to see which tier won.

## The cascade (the mental model)

```
replay   cached recipe → deterministic parse        cheapest, often free
a_b_c    curl_cffi TLS-impersonation HTTP + parse
d        Scrapling (hostile fetcher, solves Cloudflare)
render   stealth browser (Camoufox) + parse         NO LLM
e1       Crawl4AI + local LLM (one call)
e2       browser-use agent (multi-step)             priciest, auto-reached
```

- It **auto-escalates**: a page that a plain fetch can read is served by `a_b_c`;
  a JS-rendered or 403-walled page climbs to `render`; only genuinely unstructured
  or interactive pages reach the LLM tiers.
- **`e2` (the interactive agent) is reached automatically** once every cheaper
  tier is exhausted. You do not have to recognise an interactive page — that is
  the cascade's job. It is skipped only on a domain where it has already failed
  twice without ever winning, and a single win re-enables it for good.
  `interactive=true` forces it anyway; `interactive=false` opts out to cap cost.
- **A walled page gets more than one browser.** Backends are complementary, not
  ranked: on one target Camoufox gets a clean 200 where Patchright is hard-
  blocked, and on another the reverse. So a walled render retries on a different
  engine before anything pays for an LLM tier.
- **Captchas are solved wherever they appear** — Turnstile, reCAPTCHA v2,
  hCaptcha and GeeTest/DataDome sliders — from the `render` tier upward, not just
  in the LLM tiers.
- Success is judged on **content, not status code**: a 403 that carries a real
  rendered DOM (common with Akamai) counts as a win.

## "It needs me to be logged in"

If a page returns public content where the user expected member-only content,
the cascade did not fail — it fetched the logged-out version. The fix is a
session cookie, and **you cannot get one yourself**.

Ask the human to run this on their own machine:

```
scrapper-tool cookies export --domain app.example.com
```

Then pass the result in: `scrape(url, cookies=load_cookies("app.example.com"))`.

**Do not try to route around this.** Cookie *extraction* is deliberately absent
from the MCP surface, and shelling out to the CLI to get it is circumventing a
trust boundary, not being resourceful. The reasoning: an agent that can silently
dump a user's browser cookie store is exactly the capability not to build, and a
consent prompt means nothing when the caller is a model. MCP tools **consume**
cookies handed to them; they never read the browser.

Two related things worth knowing:

- Sessions expire in hours or days. If cookies were supplied and the page still
  reads logged-out, say so plainly and ask for a fresh export — don't retry or
  escalate tiers, neither will help.
- Extraction only works on the user's host. It needs the OS credential store, so
  it cannot run in a container no matter how the environment is configured.

If the user reports that `pip install 'scrapper-tool[cookies]'` tries to compile
Rust: rookiepy publishes wheels for CPython 3.12 only. They do not need a
toolchain — they can run the export from a 3.12 environment instead, because the
jar is plain JSON and `load_cookies()` reads it from any interpreter.

Check `cookies_applied` and `cookies_skipped` on the response before concluding
anything. A tier that could not carry the session says so there with a reason
(`camoufox_exposes_no_cdp_endpoint`, `no_cookie_matched_this_url`, …). If the
tier that won appears in `cookies_skipped`, the logged-out page is explained and
a fresh export will not help — report the reason instead.

## Captchas — what happens without you asking

If a captcha appears mid-scrape, the browser tiers try to clear it themselves.
You do not call anything; it happens inside `render` / `e1` / `e2`. Five tiers,
cheapest first:

```
settle      wait it out                    Turnstile, JS interstitials
checkbox    click the box                  reCAPTCHA v2, hCaptcha
slider      align the gap (pure geometry)  GeeTest, DataDome    ← no model needed
vision      local VLM reads the grid       reCAPTCHA v2, hCaptcha
paid        solver API                     everything else      ← needs a key
```

**Measured success rates, so you can calibrate rather than hope:**

| Challenge | Rate | Notes |
|---|---|---|
| reCAPTCHA v2 image grid | **3/4 – 4/5** | needs a ~27B local VLM; see below |
| GeeTest v3 slider | **~20%** | no model; failures are free, just retry |
| Turnstile / JS interstitial | varies | settles when the IP is trusted; nothing local moves it otherwise |
| reCAPTCHA v3, AWS WAF | **no** | risk score and proof-of-work — no puzzle exists to solve |
| FunCaptcha / Arkose | **no** | rotating 3D; paid tier only |

Two things follow from those numbers:

- **A captcha failure is usually not worth retrying more than once.** The tiers
  already retry internally, and the common cause is IP reputation, which a retry
  does not change.
- **Never report a captcha as "solved" because the widget disappeared.** A solved
  reCAPTCHA keeps its widget — it just turns green. The library judges by the
  response token; you should judge by whether you got the data.

### The local VLM, and the trap that wastes an afternoon

Grid solving needs a vision model around **27B** — measured, not guessed. Two
~6 GB models score 0/5 and 1/5 on the identical pipeline, including one built
specifically for spatial reasoning. `qwen/qwen3.8-27b` or `qwen/qwen3.6-27b`
score 4/5 and 5/5.

If the model driving extraction is *not* the one you want reading grids, point
the captcha tier somewhere else — they are different jobs and the best model for
one is measurably bad at the other:

```
SCRAPPER_TOOL_AGENT_MODEL=<good at page-text -> JSON>
SCRAPPER_TOOL_CAPTCHA_VISION_MODEL=<good at spatial vision>
```

Leave the second unset to reuse the first. Worth checking VRAM before splitting:
two models only help if both fit at once, otherwise the server thrashes
load/unload on every switch and one model that does both jobs wins.

Load it with an **explicit context length**:

```
lms load qwen/qwen3.8-27b --context-length 8192 --gpu max
```

Its default is 262,144 tokens, and that KV cache — not the 16 GiB of weights — is
what overflows a 24 GB card. Without the flag you get `insufficient system
resources`, which reads as "this model does not fit" and is exactly wrong.

## Clearance cookies — the tool keeps what it wins

Distinct from the session cookies above, which only a human can supply. When a
tier clears a wall or solves a captcha, the credential that bought (a
`cf_clearance`, a `datadome`) is harvested and reused, because a solve costs ~70 s
of local inference or a paid API call and throwing it away means paying twice.

- **Within a request** it is automatic: later tiers inherit it.
- **Across runs** set `persist_browser_profile_dir`. The browser keeps its own
  cookie jar there, so the next run starts already cleared.

Two properties worth trusting: the whole jar travels together (a `cf_clearance`
replayed without its `__cf_bm` sibling is often rejected), and it is scoped on
apply — domain, path, `secure` and expiry — so nothing leaks to another host and
a stale clearance is dropped rather than replayed.

## Three ways to call it — pick one

| You are… | Use | How |
|----------|-----|-----|
| An MCP client (Claude Desktop/Code, Cursor, AutoGen, LangChain) | the **MCP server** | tool `auto_scrape` |
| Writing Python | the **library** | `await scrape(url)` |
| A service in another language | the **REST sidecar** | `POST /scrape` |

All three run the *same* cascade — they can't give different answers.

---

## MCP tools

Start the server with the `scrapper-tool-mcp` command (needs `pip install
'scrapper-tool[agent]'`). Register it in your client's MCP config, e.g. `.mcp.json`:

```json
{ "mcpServers": { "scrapper-tool": { "command": "scrapper-tool-mcp", "args": [] } } }
```

**Primary tool — `auto_scrape`.** Use this for almost everything.

```
auto_scrape(
  url,                       # required
  schema_json = null,        # optional: shape to extract (see "Schemas" below)
  instruction = null,        # optional: natural-language extraction hint (LLM tiers)
  interactive = null,        # null = auto (default). true forces E2, false opts out
  browser = null,            # override the automatic choice; GET /capabilities lists valid names
  timeout_s = 120,
  hostile_only = false,      # skip the HTTP ladder, start at Scrapling (known-hard sites)
)
```

It returns `pattern_used`, `product`, `data`, `body`, `challenge_detected`,
`blocked`, and more (see "Reading the result").

**Site-level tools:**

- `map_site(url, max_urls=200, same_domain=true, include_sitemap=true)` — discover
  a site's URLs (sitemaps + page links). Cheap, no browser. Run this before a
  crawl to see how big the job is.
- `crawl_site(url, schema_json=null, depth=2, max_pages=25, concurrency=4,
  interactive=false)` — crawl breadth-first, running the full cascade per page.
  Each page benefits from the recipe learned on the first, so a crawl gets cheaper
  as it goes.

**Lower-level tools** (use only when you specifically need one, not for general
scraping): `fetch_with_ladder`, `extract_product(html)`,
`extract_microdata_price(html)`, `agent_extract`, `agent_browse`, `canary`.

---

## Python library

```python
from scrapper_tool import scrape

result = await scrape("https://store.example.com/product/123")
print(result["pattern_used"], result["product"])
```

`scrape(url, schema=None, *, interactive=None, mode="auto", browser=None,
model=None, timeout_s=None, instruction=None, persist_browser_profile_dir=None,
cookies=None)` — same params as `auto_scrape`, plus two the MCP surface does not
expose: `cookies` (see "It needs me to be logged in") and
`persist_browser_profile_dir` (reuse a browser profile across requests so
Cloudflare clearance survives).

```python
# Structured extraction with a CSS schema:
result = await scrape(url, schema={
    "baseSelector": "div.product-card",
    "fields": [
        {"name": "title", "selector": "h3", "type": "text"},
        {"name": "price", "selector": ".price", "type": "text"},
    ],
})
rows = result["data"]

# Crawl a whole site (streams a result per page):
from scrapper_tool import crawl_site
async for page in crawl_site("https://shop.example.com/", depth=2, max_pages=50):
    if page.ok:
        print(page.url, page.payload["pattern_used"])

# Just discover URLs, no scraping:
from scrapper_tool.crawl import map_site, make_ladder_fetch
result = await map_site("https://shop.example.com/", fetch=make_ladder_fetch())
```

Low-level building blocks are still exported for when the cascade is overkill:
`vendor_client`, `request_with_retry`, `request_with_ladder`, and the pattern
extractors in `scrapper_tool.patterns.{b,c}`.

---

## REST sidecar

Start with `scrapper-tool-serve` (needs `pip install 'scrapper-tool[http]'`),
default port 5792.

```
POST /scrape   {"url": "...", "schema_json": {...}, "interactive": false,
                "cookies": [ ... ]}
POST /map      {"url": "...", "max_urls": 200}
POST /crawl    {"url": "...", "depth": 2, "max_pages": 50}
GET  /health   /ready   /metrics    (Prometheus)
```

`POST /scrape` takes the same fields as `auto_scrape` and returns the same
payload. Also: `/fetch` (tier 1 only), `/extract` (force E1), `/browse` (force E2).

Two endpoints exist so you never have to guess at this tool's shape:
`GET /capabilities` lists the valid `browser` names, which tiers are usable in
this deployment, and the flags that gate them; `GET /skill` returns this
document. Over MCP the same text is the `skill://scrapper-tool` resource.

Sending `cookies` to a sidecar with no API key configured is refused with **403**
— an open port that accepts session cookies lets anyone replay someone's login
through that host. The operator sets `SCRAPPER_TOOL_HTTP_API_KEY` (and you send
`X-API-Key`), or `SCRAPPER_TOOL_HTTP_ALLOW_UNAUTH_COOKIES=1` for localhost
development. If you get that 403, report it — it is a deployment fix, not
something to retry around.

---

## Schemas — how to ask for structured data

Pass `schema_json` (MCP/REST) or `schema=` (library) in one of three forms:

1. **CSS schema** (deterministic, no LLM — prefer this for lists/tables):
   ```json
   {"baseSelector": "li.result",
    "fields": [{"name": "title", "selector": "h2", "type": "text"},
               {"name": "url", "selector": "a", "type": "attribute", "attribute": "href"}]}
   ```
   Field `type` is `text` | `attribute` (+ `attribute` key) | `html` | `int` | `float`.
2. **JSON Schema dict** — hands the page to the LLM tier to fill.
3. **Natural-language string** — e.g. `"the product name, price, and SKU"`.

With **no schema**, the cascade auto-extracts a product from JSON-LD/microdata
when present, and otherwise returns the page body/markdown.

---

## Reading the result

Every scrape returns a dict. The keys that matter:

| Key | Meaning |
|-----|---------|
| `pattern_used` | which tier won: `replay` / `a_b_c` / `d` / `render` / `e1` / `e2` |
| `product` | auto-detected product+price (JSON-LD/microdata), or null |
| `data` | rows from a CSS schema, or null |
| `raw_text` / `body` | the fetched/rendered HTML |
| `challenge_detected` | the bot-vendor that walled us (`cloudflare`, `datadome`, …) or null |
| `blocked` | true when every tier failed |
| `is_structured` | whether a real structured payload was produced |
| `escalation_log` | per-tier trace: what ran, what it cost, why it escalated |
| `requested_url` | what you asked for. Differs from `url` when you were redirected |
| `egress` | which network path was used: `{via, proxy}`. Proxy credentials are redacted |
| `cookies_applied` | tiers that carried your cookies. Present only when you sent some |
| `cookies_skipped` | `[{tier, reason}]` for tiers that ran **without** them. Same condition |
| `cookies_harvested_from` | tiers that *won* a cookie (e.g. a `cf_clearance`) and passed it forward |

**Two fields settle most questions before you guess.** `requested_url` is what
you asked for and `url` is where the request finished; if they differ, you were
redirected, and `challenge_detected: "redirect"` means you were redirected onto a
page asking you to prove you are human. `egress` names the network path that got
that answer -- the same vendor can serve one path clean HTML and hand another a
captcha in the same minute, which is not the same thing as "this vendor blocks
us".

**`blocked` now means one thing only.** It is true only on *evidence* of
blocking: a vendor signature, a challenge redirect, a known-hostile status. A
tier of ours that timed out, crashed or found nothing returns HTTP 502
`pattern_failed` instead, carrying `pattern`, `reason` and `vendor_hostile`. Only
one of those two belongs in a vendor's failure budget.

**How to act on it:** if `blocked` is true, the site defeated every tier the
cascade was allowed to run — including, by default, more than one browser engine
and the interactive agent. Read `escalation_log` before concluding anything: a
row with `reason: "not_permitted"` means *we* declined to run that tier (an
explicit `interactive=false`, a missing extra, or a learned verdict), which is a
different problem from a tier that ran and lost. Only the first is fixable in the
call. If everything genuinely ran and lost, report it as unreachable — it may
need a residential proxy, which the operator supplies via
`SCRAPPER_TOOL_PROXIES`, not something you can fix in the call. If `pattern_used` is `e1`/`e2`, a local LLM was used — that's
normal for hard/unstructured pages but slower; a CSS `schema` avoids it when the
data is in the DOM.

---

### `cookies` — what the run won

`AgentResult.cookies` carries any clearance a solve or wall-crossing minted, in
Playwright shape. Empty when nothing was won; populated even on a `blocked` run,
because a run can win a clearance and still fail to extract — which is exactly
when the next attempt wants it. Hand them back in on the next call, or persist a
browser profile dir and let the browser keep its own jar.

## Decision guide

- **"Get the data from this URL"** → `auto_scrape(url)` / `scrape(url)`. Add a
  `schema` if you want specific fields.
- **"Get every product on this site"** → `map_site` first to gauge size, then
  `crawl_site` with a `schema`.
- **"It needs me to log in / click through pages / fill a form"** → just call
  `auto_scrape`; the cascade reaches the E2 agent on its own. Pass
  `interactive=true` only to force it past a learned "E2 never works here".
- **"It's a known-hard site (Cloudflare/Akamai)"** → just call `auto_scrape`; the
  cascade escalates on its own. Optionally `hostile_only=true` to skip the
  ladder and save ~2 s.
- **"I already have the HTML"** → `extract_product(html)` /
  `extract_microdata_price(html)`, no fetch.

## Refused targets — what `url_not_allowed` means

Since v2.2.1 every surface vets a URL **before** issuing a request. If you get
`url_not_allowed`, the tool declined to fetch on your behalf; the site was never
contacted. **Do not retry it, and do not escalate to another tier** — every tier
refuses the same target.

You will see it as REST `403` with `{"error": "url_not_allowed", "reason", "remedy"}`,
or on MCP as a normal result carrying `error_code: "url_not_allowed"` and a
`remedy`. Note `blocked` stays `false`: this is not an anti-bot wall, so the
usual "climb a tier" reflex is wrong here.

What gets refused, and what to do:

| `reason` | Meaning | What to do |
|---|---|---|
| `metadata` | A cloud metadata endpoint (`169.254.169.254` and friends) | Nothing. These hand out credentials; there is no legitimate scrape here. |
| `private_ip`, `loopback`, `link_local`, `cgnat` | Private/internal address | If the user genuinely wants an internal target, they set `SCRAPPER_TOOL_URL_GUARD_ALLOW=<host-or-cidr>`. Ask; do not disable the guard. |
| `special_tld` | `.local`, `.internal`, `.onion`, `.corp`… | Same — allowlist the specific host if intended. |
| `scheme` | Not `http(s)` — `file:`, `data:`, `gopher:` | Usually a malformed URL. Re-read it. |
| `userinfo` | Credentials in the URL (`user@host`) | Strip them; the whole URL is refused rather than parsed, because `https://real.com@169.254.169.254/` reads as one host and fetches another. |
| `uninterceptable_tier` | The operator set `SCRAPPER_TOOL_URL_GUARD_STRICT=1`, which refuses tiers whose requests cannot be vetted (`d`, `render`, `e1`, `e2`, `obscura`) | Report it. On a protected site this means the scrape cannot proceed without the operator relaxing that setting — it is a deliberate containment choice, not a bug. |

The escape hatch is always **allowlist the target**, never turn the guard off:
`SCRAPPER_TOOL_URL_GUARD=0` disables it for every URL in the process.

## Gotchas

- The LLM tiers (`e1`/`e2`) need the `[llm-agent]` extra and a running local LLM
  (Ollama by default, `SCRAPPER_TOOL_AGENT_LLM` / `_MODEL` / `_OLLAMA_URL` to
  configure). Without them the cascade still runs A/B/C/D/render and reports
  `hostile_skipped` / stops rather than crashing.
- `render` (Camoufox) needs `[llm-agent]` installed and `camoufox fetch` run once.
- The cascade caches learned recipes and per-domain tier memory under a temp dir;
  set `SCRAPPER_TOOL_RECIPE_DIR` to relocate, or `SCRAPPER_TOOL_RECIPE_CACHE=0` /
  `SCRAPPER_TOOL_DOMAIN_POLICY=0` to disable.
- The toolkit cannot manufacture IP reputation. If a site blocks every tier from
  your IP, that's an IP-trust limit — a residential/mobile proxy is the fix, not
  a different tier.
- **When ONE url fails, run `scrapper-tool diagnose <url>` before theorising.**
  It fetches the page with every impersonation profile and a couple of URL
  variants, then prints a verdict: `wrong_url` (every profile got a 404 -- the
  path is wrong, the vendor is innocent), `reachable`, `challenged` (a real wall
  on this network path), or `unreachable`. Two of the five symptoms in the report
  that prompted this command were wrong paths read as hostility.
- **When several tiers fail at once, suspect the install before the site.** Ask
  the user to run `scrapper-tool doctor`: it reports each tier as ok / degraded /
  missing / blocked with the one command that fixes it. The common causes are a
  browser *module* that imports while its *binary* was never downloaded, and a
  local LLM that isn't running. `e2 | blocked` on the default `camoufox` backend
  is a *doctor* artefact, not a scrape fault: Firefox has no CDP, so E2 cannot
  attach to Camoufox — but a real scrape now picks a CDP-capable backend for the
  E2 leg by itself, leaving D and E1 on Camoufox where its stealth counts.

- **`docker run … scrapper-tool doctor` does not work.** The image entrypoint is
  `scrapper-tool-serve`, so those tokens are parsed as server flags and it exits
  2. Use `docker run --rm --entrypoint scrapper-tool <image> doctor --json`.
- **The captcha grid tier uses a *different* model from extraction** (`SCRAPPER_TOOL_CAPTCHA_VISION_MODEL`, default `qwen3.8-27b-apex`). Grids want a large VLM; extraction wants a small instruction-follower, and one model loses at whichever job it was not picked for. If the grid tier never solves anything, check `scrapper-tool doctor`'s `captcha_vision_model` row — a large model that this host cannot serve degrades quietly by design.
- Captcha solving is best-effort and its rates are above. If a run needs a
  guaranteed solve, configure a paid solver key (`SCRAPPER_TOOL_CAPTCHA_KEY`);
  the free tiers try first regardless, so the key only costs money when they fail.

## Reference

- Full settings: `docs/SETTINGS.md`
- The URL guard in full, incl. what is *not* covered: `docs/SETTINGS.md#target-url-guard-ssrf-protection-v221`
- What has shipped and what is knowingly unfinished: `docs/PROGRESS.md`
- Per-tool MCP wiring for each framework: `docs/agent-integration.md`
- The cascade tiers in depth: `docs/patterns/`
- What is tested and how to reproduce it: `docs/TESTING.md`
- Measured captcha/model results: `docs/MODEL_RESEARCH.md`
