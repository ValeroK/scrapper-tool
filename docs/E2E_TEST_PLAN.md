# End-to-end test plan — scrapper-tool

A complete walkthrough that exercises **every capability** of `scrapper-tool`
against real sites, in **three execution modes** (library / Docker / MCP),
using **LM Studio on the host** as the local LLM. Designed to be run by an
operator on demand — **NOT in CI** (it costs real network egress, real LLM
inference time, and real solver credits if you opt in).

> **Total wall-clock estimate:** ~45 min for the headline path on a 16 GB
> GPU. Add ~20 min if you exercise the captcha tier-2 paid path.
> Add ~15 min for the MCP-from-Claude-Code section.

---

## 0. What this plan covers

| Capability | Where exercised |
|---|---|
| Pattern A (JSON API) | All three modes |
| Pattern B (embedded JSON) | All three modes |
| Pattern C (CSS / microdata) | All three modes |
| Pattern D (Scrapling hostile) | Library + Docker + MCP |
| Pattern E1 (`agent_extract`) | Library + Docker + MCP |
| Pattern E2 (`agent_browse`) | Library + Docker + MCP |
| LM Studio integration (host) | All three modes |
| External LLM via `host.docker.internal` | Docker + MCP |
| Captcha cascade — Tier 0 (Camoufox auto-pass) | Library + Docker |
| Captcha cascade — Tier 1 (Theyka) | Library |
| Captcha cascade — Tier 2 paid (CapSolver) | Optional, gated |
| Behavior policy: humanlike / fast / off | Library |
| Browser backends: Camoufox / Patchright / Zendriver / Botasaurus / Scrapling | Library |
| Fingerprint generator: Browserforge | Library |
| TLS impersonation ladder | All three modes |
| MCP server stdio framing | MCP mode |
| MCP `agent_extract` / `agent_browse` tools | MCP mode |
| Robots.txt respect | Library |
| Schema validation (pydantic / dict / natural-language) | Library + MCP |
| Error taxonomy (BlockedError / AgentBlockedError / AgentTimeoutError / AgentLLMError / CaptchaSolveError) | Library |
| Body / screenshot truncation in MCP responses | MCP |

What this plan **does not** cover (out of scope by design):
- Sites behind extreme Kasada / DataDome v2 / Shape Security — bypass is an arms race; the lib offers escalation, not a guarantee.
- 2FA / SMS / email-verification flows.
- Sustained-load / rate-limit testing — single-shot probes only.

---

## 1. Prerequisites

### Hardware

| Resource | Minimum | Recommended |
|---|---|---|
| RAM | 16 GB | 32 GB |
| GPU VRAM | 8 GB (qwen3-vl:4b) | 16 GB (qwen3-vl:8b) |
| Disk | ~5 GB free | ~10 GB free |
| Network | Stable broadband | — |

If you don't have a GPU, you can still run E1 / E2 but expect 60+ s per LLM
call. Use `qwen3-vl:2b` and accept the lower extraction quality.

### Software

- **Docker Desktop** (Mac / Windows) or Docker + Compose (Linux).
- **Python 3.13** + **uv** ≥ 0.5 (for library mode).
- **LM Studio** ≥ 0.3.5 — https://lmstudio.ai/. Linux users can use
  `llama.cpp server` instead and follow the LM Studio steps adapted (port 8080).
- Optional: a paid CAPTCHA-solver API key (CapSolver / NopeCHA / 2Captcha)
  for Tier-2 cascade tests. Free dev tiers exist on NopeCHA.

### Test sites (all public, stable, scraping-friendly)

| Site | Used for | Why it's stable |
|---|---|---|
| `https://example.com` | Smoke test for HTTP, browser launch, Pattern A/E2 | RFC-2606 reserved, never goes away |
| `https://httpbin.org/anything` | Pattern A — echo, header verification | Postman's developer playground |
| `https://jsonplaceholder.typicode.com/posts/1` | Pattern A — JSON API anonymous fetch | Public REST mock, no auth |
| `https://dummyjson.com/products/1` | Pattern A — schema-typed JSON product | Public JSON mock with product data |
| `https://fakestoreapi.com/products/1` | Pattern A — alternative product JSON | Public JSON mock |
| `https://schema.org/Product` | Pattern B — JSON-LD walker | schema.org publishes example pages for tools |
| `https://quotes.toscrape.com/` | Pattern C / Pattern E1 — listing extraction | Purpose-built scraping practice (toscrape.com) |
| `https://quotes.toscrape.com/js/` | Pattern E1 — JS-rendered listing | Same site, JS-rendered variant |
| `https://books.toscrape.com/` | Pattern E1 — product catalogue | Purpose-built |
| `https://nopecha.com/demo/cloudflare` | Captcha Tier 0/1, Pattern D, Pattern E1 | Public Turnstile demo |
| `https://nopecha.com/demo/recaptcha` | Captcha Tier 2 (paid) | Public reCAPTCHA demo |
| `https://nopecha.com/demo/hcaptcha` | Captcha Tier 2 (paid) | Public hCaptcha demo |
| `https://bot.sannysoft.com/` | Stealth fingerprint visualisation | Bot-detection test page |
| `https://abrahamjuliot.github.io/creepjs/` | Fingerprint scoring (manual review) | CreepJS detector |
| `https://arh.antoinevastel.com/bots/areyouheadless` | Headless detection | Public detector |

Be a good citizen: don't loop these in tight cycles, don't run multiple
parallel agents at the same target, and respect each site's `/robots.txt`.
The plan below issues at most one request per probe.

---

## 2. Set up LM Studio

### 2.1 Install + load a model

1. Install LM Studio (https://lmstudio.ai/) and open it.
2. **Discover** tab → search **`qwen3-vl-8b-instruct`** (recommended, 16 GB
   VRAM at Q4_K_M). On 8 GB cards search `qwen3-vl-4b-instruct`. On CPU
   only, search `qwen2.5-vl-3b-instruct`.
3. Download. The card will show ~6 GB on disk for the 8B Q4_K_M variant.
4. **My Models** → click your model → **Load** with default settings.
5. **Developer** (or **Local Server** in older builds) → click **Start
   Server**. Note the port — default `1234`.
6. Confirm the model name LM Studio is exposing — you'll need it
   verbatim (e.g. `qwen3-vl-8b-instruct`).

### 2.2 Sanity check the OpenAI-compatible endpoint

```bash
curl -s http://localhost:1234/v1/models | python -m json.tool
```

Expected: a JSON object with `data: [{ id: "qwen3-vl-8b-instruct", ... }]`.

```bash
curl -s http://localhost:1234/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-vl-8b-instruct","messages":[{"role":"user","content":"Reply with the word OK"}]}' \
  | python -m json.tool
```

Expected: a `choices[0].message.content` that contains `OK`. If it doesn't,
the rest of this plan won't work.

### 2.3 Configure scrapper-tool to use LM Studio

Create a `.env` next to `docker-compose.yml` (and source it in your shell
for library mode):

```env
SCRAPPER_TOOL_AGENT_LLM=openai_compat
SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://localhost:1234
SCRAPPER_TOOL_AGENT_MODEL=qwen3-vl-8b-instruct

# Docker variant — host.docker.internal reaches the host:
# SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:1234
```

For Docker mode, the same `.env` file is auto-loaded by compose. Use
`host.docker.internal` instead of `localhost` for the Docker URL.

---

## 3. Mode 1 — Library (SDK)

### 3.1 Install

```bash
git clone <this repo> && cd scrapper-tool
uv pip install -e ".[full,agent]"
camoufox fetch                    # ~300 MB
patchright install chromium       # ~250 MB
```

Sanity check:

```bash
uv run python -c "
import scrapper_tool, scrapper_tool.agent
from scrapper_tool import errors
print('version:', scrapper_tool.__version__)
print('agent loaded:', scrapper_tool.agent.__name__)
"
```

Expected: `version: 1.0.0`.

### 3.2 Test 3.A — Pattern A (JSON API)

```python
# scripts/e2e/test_pattern_a.py
import asyncio
import httpx
from scrapper_tool import vendor_client, request_with_retry

async def main() -> None:
    async with vendor_client() as client:
        resp = await request_with_retry(client, "GET", "https://dummyjson.com/products/1")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 1
    assert "title" in payload
    print("Pattern A ✅", payload["title"])

asyncio.run(main())
```

Expected output: `Pattern A ✅ <some product name>`.

### 3.3 Test 3.B — Pattern B (embedded JSON / extruct)

```python
# scripts/e2e/test_pattern_b.py
import asyncio
from scrapper_tool import vendor_client, request_with_retry
from scrapper_tool.patterns.b import extract_product_offer

async def main() -> None:
    # schema.org publishes example HTML with JSON-LD baked in.
    # If this URL ever stops carrying a Product block, swap to a
    # different schema.org developer example page.
    url = "https://schema.org/Product"
    async with vendor_client() as client:
        resp = await request_with_retry(client, "GET", url)
    product = extract_product_offer(resp.text, base_url=url)
    if product is None:
        print("Pattern B — no Product on schema.org/Product today; "
              "swap to a known-good page like a real e-commerce product URL.")
        return
    assert product.name
    print("Pattern B ✅", product.name, product.price, product.currency)

asyncio.run(main())
```

If schema.org rotates the example, run this against a real product page on
a site you have permission to scrape.

### 3.4 Test 3.C — Pattern C (microdata / selectolax)

```python
# scripts/e2e/test_pattern_c.py
from scrapper_tool.patterns.c import extract_microdata_price

html = """
<html><body>
  <span itemtype="http://schema.org/Offer">
    <meta itemprop="price" content="19.99">
    <meta itemprop="priceCurrency" content="USD">
  </span>
</body></html>
"""
result = extract_microdata_price(html)
assert result is not None
price, currency = result
assert str(price) == "19.99"
assert currency == "USD"
print("Pattern C ✅", price, currency)
```

### 3.5 Test 3.D — Pattern D (Scrapling hostile)

> **Heavy.** Launches a Playwright Chromium. Skip if you don't need it.

```python
# scripts/e2e/test_pattern_d.py
import asyncio
from scrapper_tool.patterns.d import hostile_client

async def main() -> None:
    async with hostile_client(headless=True, block_resources=True) as fetcher:
        # Pick a target you have permission to scrape. Public Turnstile demo:
        resp = await fetcher.async_fetch(
            "https://nopecha.com/demo/cloudflare",
            solve_cloudflare=True,
        )
    assert resp.status == 200, f"unexpected status {resp.status}"
    body = resp.html_content
    assert len(body) > 1000, "body too short — likely blocked"
    assert "challenge" not in body.lower() or "passed" in body.lower()
    print("Pattern D ✅ rendered", len(body), "bytes")

asyncio.run(main())
```

Common failure: Scrapling's Turnstile auto-solve doesn't always win on the
first try. Re-run once. If it consistently fails, escalate to Pattern E
(test 3.F below).

### 3.6 Test 3.E1 — `agent_extract` (Pattern E1)

```python
# scripts/e2e/test_pattern_e1.py
import asyncio
from scrapper_tool.agent import AgentConfig, agent_extract

async def main() -> None:
    schema = {
        "type": "object",
        "properties": {
            "quotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "author": {"type": "string"},
                    },
                    "required": ["text", "author"],
                },
            }
        },
        "required": ["quotes"],
    }
    cfg = AgentConfig.from_env().merged(
        browser="patchright",      # fast mode
        captcha_solver="none",
        timeout_s=180.0,
    )
    result = await agent_extract(
        "https://quotes.toscrape.com/",
        schema=schema,
        config=cfg,
        instruction="Extract every quote on the page.",
    )
    assert not result.blocked, result.error
    assert result.data is not None
    quotes = result.data["quotes"] if isinstance(result.data, dict) else result.data
    print(f"Pattern E1 ✅ extracted {len(quotes)} quotes in {result.duration_s:.1f} s")
    print("First quote:", quotes[0])

asyncio.run(main())
```

Expected: ≥ 5 quotes returned. LM Studio's GPU log will show the inference.

### 3.6.1 Test 3.E1-camoufox — same with the strongest stealth backend

```bash
SCRAPPER_TOOL_AGENT_BROWSER=camoufox \
  uv run python scripts/e2e/test_pattern_e1.py
```

Expected: same data, ~3-5× slower (~30-60 s typical).

### 3.6.2 Test 3.E1-pydantic-schema — pydantic class as schema

```python
# scripts/e2e/test_pattern_e1_pydantic.py
import asyncio
from pydantic import BaseModel
from scrapper_tool.agent import AgentConfig, agent_extract

class Book(BaseModel):
    title: str
    price: float

class Page(BaseModel):
    books: list[Book]

async def main() -> None:
    cfg = AgentConfig.from_env().merged(browser="patchright", captcha_solver="none")
    result = await agent_extract(
        "https://books.toscrape.com/",
        schema=Page,
        config=cfg,
        instruction="Extract all books shown on the page.",
    )
    print(result.data)

asyncio.run(main())
```

### 3.7 Test 3.E2 — `agent_browse` (Pattern E2, interactive)

```python
# scripts/e2e/test_pattern_e2.py
import asyncio
from scrapper_tool.agent import AgentConfig, agent_browse

async def main() -> None:
    cfg = AgentConfig.from_env().merged(
        browser="patchright",
        captcha_solver="none",
        max_steps=10,
        timeout_s=240.0,
    )
    result = await agent_browse(
        "https://quotes.toscrape.com/",
        instruction=(
            "Click the 'Next' button at the bottom of the page to go to "
            "page 2, then return a JSON object {page: 2, count: <number "
            "of quotes shown on page 2>}."
        ),
        config=cfg,
    )
    assert not result.blocked
    print(f"Pattern E2 ✅ {result.steps_used} steps, "
          f"{result.duration_s:.1f} s, data={result.data}")

asyncio.run(main())
```

Expected: agent navigates to page 2 and reports the count. Local 8B models
sometimes return slightly off-shape JSON — run twice; if it's consistently
wrong, drop to a smaller `max_steps` and a more directive instruction.

### 3.8 Test 3.F — Captcha cascade (Tier 0 free OSS)

```python
# scripts/e2e/test_captcha_tier0.py
import asyncio
from scrapper_tool.agent import AgentConfig, agent_extract

async def main() -> None:
    cfg = AgentConfig.from_env().merged(
        browser="camoufox",        # Tier 0 only works on Camoufox
        captcha_solver="auto",
        timeout_s=240.0,
    )
    result = await agent_extract(
        "https://nopecha.com/demo/cloudflare",
        schema="Return a JSON object describing whether the page rendered past the challenge.",
        config=cfg,
    )
    if result.blocked:
        print("Captcha Tier 0 ❌ Camoufox didn't auto-pass today; "
              "this can happen — escalate to Tier 1 (Theyka).")
        return
    print(f"Captcha Tier 0 ✅ rendered, {result.duration_s:.1f} s")

asyncio.run(main())
```

### 3.9 Test 3.G — Captcha Tier 2 (paid, opt-in)

> **Costs real money.** Skip unless you've configured a paid solver.

```bash
export SCRAPPER_TOOL_CAPTCHA_KEY=<your_capsolver_key>
export SCRAPPER_TOOL_CAPTCHA_PAID_FALLBACK=capsolver
```

```python
# scripts/e2e/test_captcha_tier2.py
import asyncio
from scrapper_tool.agent import AgentConfig, agent_extract

async def main() -> None:
    cfg = AgentConfig.from_env().merged(
        browser="patchright",          # paid solver works regardless of backend
        captcha_solver="auto",
        timeout_s=300.0,
    )
    result = await agent_extract(
        "https://nopecha.com/demo/recaptcha",
        schema="Return JSON describing whether the page passed the challenge.",
        config=cfg,
    )
    print(result.data, "blocked=", result.blocked)

asyncio.run(main())
```

Sanity-check the cost on your CapSolver dashboard after the run — you
should see one solve charge.

### 3.10 Test 3.H — Behavior policy comparison

Quick A/B/C of `humanlike`, `fast`, `off`. Same target, three runs:

```bash
for behavior in humanlike fast off; do
  echo "=== behavior=$behavior ==="
  SCRAPPER_TOOL_AGENT_BEHAVIOR=$behavior \
  SCRAPPER_TOOL_AGENT_BROWSER=patchright \
    uv run python scripts/e2e/test_pattern_e1.py
done
```

Expected: roughly same data, `humanlike` is 1.5-3 s slower than `fast`,
`off` matches `fast`. If `humanlike` is dramatically slower than `fast`,
the timing distribution config is correct.

### 3.11 Test 3.I — TLS impersonation ladder

```bash
uv run scrapper-tool canary https://example.com --json
```

Expected: a JSON dict with `winning_profile: "chrome133a"` (or whichever
profile wins first) and `exit_code: 0`. This is the same logic as the
`canary` MCP tool.

### 3.12 Test 3.J — Stealth fingerprint visualisation (manual)

Open `https://bot.sannysoft.com/` in two ways:
1. A **headful** Camoufox session (run `agent_browse` with
   `SCRAPPER_TOOL_AGENT_HEADFUL=1` and a no-op instruction).
2. A vanilla Playwright Chromium for comparison.

Eyeball the table — Camoufox should be all-green where vanilla Playwright
has multiple red rows (`navigator.webdriver`, plugins length, language
inconsistencies). Take a screenshot for the report.

### 3.13 Test 3.K — Error taxonomy

```python
# scripts/e2e/test_errors.py
import asyncio
from scrapper_tool import BlockedError
from scrapper_tool.agent import AgentConfig, agent_extract
from scrapper_tool.errors import AgentBlockedError, AgentLLMError

async def main() -> None:
    # 1. AgentBlockedError must be caught by `except BlockedError`.
    try:
        raise AgentBlockedError("simulated")
    except BlockedError as exc:
        print("✅ AgentBlockedError caught by BlockedError:", exc)

    # 2. Bad LLM URL → AgentLLMError, not silent retry.
    cfg = AgentConfig(
        llm="openai_compat",
        ollama_url="http://127.0.0.1:1",   # nothing listens here
        model="qwen3-vl-8b-instruct",
        captcha_solver="none",
        browser="patchright",
        timeout_s=10.0,
    )
    try:
        await agent_extract("https://example.com", schema={}, config=cfg)
        print("❌ expected AgentLLMError")
    except AgentLLMError as exc:
        print("✅ Bad URL → AgentLLMError:", exc)

asyncio.run(main())
```

---

## 4. Mode 2 — Docker

### 4.1 Build

```bash
docker compose build scrapper-tool                 # default — Pattern A-E + MCP
# Bake in Camoufox too (heavier image but enables Tier 0 captcha):
INSTALL_CAMOUFOX=1 docker compose build scrapper-tool
```

### 4.2 Sanity check the image talks to LM Studio

`.env` next to `docker-compose.yml`:

```env
SCRAPPER_TOOL_AGENT_LLM=openai_compat
SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:1234
SCRAPPER_TOOL_AGENT_MODEL=qwen3-vl-8b-instruct
```

```bash
docker compose run --rm scrapper-tool python -c "
import asyncio
from scrapper_tool.agent.backends.llm import OpenAICompatBackend
asyncio.run(OpenAICompatBackend(
    model='qwen3-vl-8b-instruct',
    base_url='http://host.docker.internal:1234',
).probe())
print('LLM reachable from container ✅')
"
```

Expected: `LLM reachable from container ✅`. If this fails, Docker can't
reach the host — see the troubleshooting section below.

### 4.3 Re-run the library tests inside the container

The same `scripts/e2e/*.py` files mount into the container — copy them in
or build them into a small fixture image:

```bash
docker compose run --rm \
  -v "$(pwd)/scripts:/work/scripts" \
  -e PYTHONPATH=/work \
  --entrypoint python \
  scrapper-tool \
  /work/scripts/e2e/test_pattern_e1.py
```

Run each of `test_pattern_a.py`, `test_pattern_b.py`, `test_pattern_c.py`,
`test_pattern_d.py`, `test_pattern_e1.py`, `test_pattern_e2.py`,
`test_captcha_tier0.py` (only if `INSTALL_CAMOUFOX=1` was set during build),
and `test_errors.py` this way.

### 4.4 Volume size + browser binary check

```bash
docker compose run --rm scrapper-tool sh -c "
echo '== Patchright Chromium =='
patchright install chromium 2>&1 | tail -2
echo '== Playwright Chromium (Scrapling) =='
playwright install chromium 2>&1 | tail -2
echo '== Camoufox =='
test -f \$HOME/.cache/camoufox/firefox || echo 'NOT installed (ok if INSTALL_CAMOUFOX=0)'
"
```

Expected: both Chromiums report "is already installed". Camoufox present
only if you built with `INSTALL_CAMOUFOX=1`.

### 4.5 MCP-server-from-Docker reachability

```bash
docker compose run --rm -T scrapper-tool < /dev/null
```

Expected: hangs awaiting stdin (this is correct — MCP is JSON-RPC over
stdio). Kill with Ctrl+C. The fact that the entrypoint started without
error means the image is wired.

### 4.6 Live-canary profile (full Pattern E suite inside Docker)

```bash
SCRAPPER_TOOL_AGENT_LLM=openai_compat \
SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:1234 \
SCRAPPER_TOOL_AGENT_MODEL=qwen3-vl-8b-instruct \
docker compose --profile live up canary
```

Expected: the `tests/integration/test_agent_live.py` suite runs against
LM Studio. Most tests expect `blocked=False`. Some are `pytest.skip`-able
when the underlying conditions aren't met (Camoufox not installed, etc.).

---

## 5. Mode 3 — MCP (driven by Claude Code or another agent)

### 5.1 Wire the MCP server into Claude Code

Put one of the two stanzas below in your `.mcp.json` (project) or
`claude_desktop_config.json` (global).

**Option A — local install (faster startup):**

```json
{
  "mcpServers": {
    "scrapper-tool-e2e": {
      "command": "scrapper-tool-mcp",
      "args": [],
      "env": {
        "SCRAPPER_TOOL_AGENT_LLM": "openai_compat",
        "SCRAPPER_TOOL_AGENT_OLLAMA_URL": "http://localhost:1234",
        "SCRAPPER_TOOL_AGENT_MODEL": "qwen3-vl-8b-instruct",
        "SCRAPPER_TOOL_AGENT_BROWSER": "patchright"
      }
    }
  }
}
```

**Option B — Dockerized (fully isolated):**

```json
{
  "mcpServers": {
    "scrapper-tool-e2e": {
      "command": "docker",
      "args": [
        "compose", "-f", "/abs/path/to/scrapper-tool/docker-compose.yml",
        "run", "--rm", "-T", "scrapper-tool"
      ]
    }
  }
}
```

Restart Claude Code. The six tools should appear in the tool palette:

- `fetch_with_ladder`
- `extract_product`
- `extract_microdata_price`
- `canary`
- `agent_extract`
- `agent_browse`

### 5.2 Conversational test scripts

Run each prompt below in a fresh Claude Code session and verify the
expected tool was called and produced the expected shape. The "expected"
column is what the agent should **report back to you** — the underlying
tool result is shown when Claude Code expands the tool call.

| # | Prompt to type into Claude | Tool the agent should call | Expected result |
|---|---|---|---|
| 5.A | *"Use canary to walk the impersonation ladder against https://example.com and tell me which profile wins."* | `canary` | A profile name (`chrome133a` typically), status 200 |
| 5.B | *"Use fetch_with_ladder on https://httpbin.org/anything?msg=hello and return the body's first 200 characters."* | `fetch_with_ladder` | JSON echo containing `msg=hello`, `winning_profile=...`, status 200 |
| 5.C | *"Fetch this Schema.org Product example HTML and use extract_product to give me the price: \<paste the HTML below\>"* (paste a small `<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Pen","offers":{"@type":"Offer","price":"3.99","priceCurrency":"USD"}}</script>`) | `extract_product` | A `ProductOffer` dict with `name=Pen`, `price=3.99`, `currency=USD` |
| 5.D | *"Use extract_microdata_price on this HTML: \<paste a microdata snippet\>"* | `extract_microdata_price` | `{price: ..., currency: ...}` |
| 5.E | *"Use agent_extract on https://quotes.toscrape.com/ and give me the first three quotes as JSON. Use schema {type:object, properties:{quotes:{type:array,items:{type:object,properties:{text:{type:string},author:{type:string}}}}}}."* | `agent_extract` | A list of ≥3 quotes; non-empty `text` and `author` |
| 5.F | *"Use agent_browse on https://example.com — click the only link and tell me the destination page's main heading."* | `agent_browse` | Reports something about IANA |

### 5.3 Shape-check the MCP tool envelopes

For test 5.E, expand the tool call in Claude Code and verify:
- `mode: "extract"`
- `data` is non-null and matches the schema shape
- `screenshots` is `null` (E1 doesn't capture screenshots by default)
- `actions` has exactly one entry with `step=1, action="extract"`
- `blocked` is `false`
- `tokens_used` reflects what LM Studio reported (may be 0 if LM Studio
  doesn't report usage; that's expected)

For test 5.F, expand and verify:
- `mode: "browse"`
- `actions` has multiple entries (goto, click, extract)
- `screenshots` MAY contain ≤3 base64-encoded PNG strings (depends on
  whether browser-use captured screenshots for that run)
- `steps_used` ≥ 2 (navigation + click + final extract)
- `final_url` ends with `iana.org` or similar

### 5.4 Body / screenshot truncation check

```text
You: Use fetch_with_ladder on https://en.wikipedia.org/wiki/Web_scraping
     and tell me whether the body was truncated.
```

Wikipedia's HTML is well over 64 KB. Expected: `truncated: true` in the
tool's result.

### 5.4.1 Automated MCP session test — agent simulation

For users who don't want to drive the prompts above by hand, the repo
ships [`scripts/e2e/test_mcp_session.py`](../scripts/e2e/test_mcp_session.py)
which uses the official `mcp` Python client SDK to spawn
`scrapper-tool-mcp` as a sibling subprocess and exercise every tool
over stdio JSON-RPC — exactly the wire format Claude Desktop uses.

Run it inside Docker (recommended — Pattern E browser launch needs Linux):

```bash
cat scripts/e2e/test_mcp_session.py | docker compose run --rm -T \
  -e SCRAPPER_TOOL_AGENT_LLM=openai_compat \
  -e SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:6543 \
  -e SCRAPPER_TOOL_AGENT_MODEL=google/gemma-4-e4b \
  -e SCRAPPER_TOOL_AGENT_BROWSER=patchright \
  --entrypoint python scrapper-tool -
```

Expected output (each line is one tool exercised through the MCP boundary):

```
[5.0] tools advertised: ['agent_browse', 'agent_extract', 'canary', ...]
[5.0] [OK] all 6 tools present
[5.A] [OK] winning_profile=chrome133a
[5.B] [OK] status=200 winning_profile=chrome133a truncated=False
[5.C] [OK] name='Pen' price=3.99 currency=USD
[5.D] [OK] {'price': '19.99', 'currency': 'USD'}
[5.4] [OK] truncated=True body_bytes=65180
[5.E] [OK] mode=extract quotes=10 duration=13.5s steps_used=1
[5.F] [OK] mode=browse steps_used=5 duration=40s data={'page': 2, 'count': 10}
=== MCP session E2E COMPLETE - all 7 tool checks passed ===
```

### 5.5 Lazy-import error-envelope check

> Only meaningful if you also have a `[hostile]`-only or `[agent]`-only
> install configured as a separate MCP server.

```text
You: Use agent_extract on https://example.com.
```

If the connected MCP server doesn't have `[llm-agent]` installed, expected
result:

```json
{ "blocked": false, "data": null,
  "error": "scrapper-tool[llm-agent] extra not installed.…" }
```

The agent should report the error gracefully, not crash.

---

## 6. Per-backend matrix (Pattern E only)

For each combination below, run `scripts/e2e/test_pattern_e1.py`. Mark
result.

| Browser | LLM | Behavior | Status / notes |
|---|---|---|---|
| Patchright | LM Studio openai_compat | humanlike | |
| Patchright | LM Studio openai_compat | fast | |
| Camoufox | LM Studio openai_compat | humanlike | (slow — expect 30-60s) |
| Zendriver | LM Studio openai_compat | humanlike | requires `[zendriver-backend]` |
| Botasaurus | LM Studio openai_compat | humanlike | requires `[botasaurus-backend]` |
| Scrapling | LM Studio openai_compat | humanlike | reuses Pattern D fetcher |
| Patchright | Ollama (host) | humanlike | switch `SCRAPPER_TOOL_AGENT_LLM=ollama` and URL |
| Patchright | llama.cpp `server` (host:8080) | humanlike | `SCRAPPER_TOOL_AGENT_LLM=llama_cpp` |
| Patchright | vLLM (host:8000) | humanlike | `SCRAPPER_TOOL_AGENT_LLM=vllm` |

Treat the first six rows (browser variation) as Pattern E correctness
checks, and the bottom four (LLM-backend variation) as the LM-Studio
documentation tax — verify each external LLM works, even if you only
intend to use one in production.

---

## 7. Performance baseline (informational)

Run `scripts/e2e/test_pattern_e1.py` against `https://quotes.toscrape.com/`
with each browser, three runs each, and record `result.duration_s`. Useful
for spotting regressions later.

| Backend | p50 | p95 |
|---|---|---|
| Patchright | ~5-15 s | ~25 s |
| Camoufox | ~25-50 s | ~80 s |
| Zendriver | ~5-10 s | ~20 s |

These numbers assume LM Studio with qwen3-vl-8b-instruct on a 16 GB GPU.
On CPU, multiply by 5-10×.

---

## 8. Failure-mode tests (do these last — they exercise error paths)

| Failure | How to trigger | Expected behavior |
|---|---|---|
| LM Studio not running | Stop LM Studio's local server, run any E1/E2 test | `AgentLLMError` raised at session start (probe-on-entry) |
| Wrong model name | `SCRAPPER_TOOL_AGENT_MODEL=does-not-exist` | `AgentLLMError` from probe |
| Site blocks even Camoufox | Run E1 against a Cloudflare-Enterprise site you have permission to test | `AgentBlockedError` (which is also caught by `except BlockedError`) |
| Schema validation fails | Use a strict pydantic schema that requires a field the page doesn't expose | `AgentResult.error == "schema-validation-failed"`, `data["_raw"]` populated, NO exception |
| Step budget exhausted in E2 | Set `SCRAPPER_TOOL_AGENT_MAX_STEPS=2` and ask for a 5-step task | `AgentResult.error == "no-match"`, `steps_used == 2` |
| Timeout | Set `SCRAPPER_TOOL_AGENT_TIMEOUT_S=5` and run E1 with Camoufox | `AgentTimeoutError` |
| Captcha encountered, no solver | Set `SCRAPPER_TOOL_CAPTCHA_SOLVER=none` and target a Turnstile page | `AgentBlockedError("captcha-encountered")` |
| Robots.txt disallow | Find a site whose robots.txt forbids your agent UA, run with `respect_robots=true` (default) | Agent refuses before fetch |

---

## 9. Reporting template

After running, fill in:

```markdown
## scrapper-tool E2E test report

- Date: 2026-MM-DD
- Operator: <your name>
- LLM: LM Studio <port> with <model>
- GPU: <model, VRAM>
- scrapper-tool version: 1.0.0

### Results

| # | Test | Mode | Result | Duration | Notes |
|---|---|---|---|---|---|
| 3.A | Pattern A | Library | ✅ | 0.3 s | |
| 3.B | Pattern B | Library | ✅ | 0.5 s | |
| 3.C | Pattern C | Library | ✅ | <0.1 s | |
| 3.D | Pattern D | Library | ✅ | 28 s | |
| 3.E1 | Pattern E1 (Patchright) | Library | ✅ | 12 s | |
| 3.E1-camoufox | Pattern E1 (Camoufox) | Library | ✅ | 47 s | |
| 3.E1-pydantic | Pattern E1 + pydantic schema | Library | ✅ | 11 s | |
| 3.E2 | Pattern E2 | Library | ✅ | 95 s | |
| 3.F | Captcha Tier 0 | Library | ⚠️ | 32 s | site rotated; 1/3 failed |
| 3.G | Captcha Tier 2 | Library | (skipped) | — | no API key |
| 3.H | Behavior policy A/B/C | Library | ✅ | 38 s total | |
| 3.I | TLS ladder canary | Library | ✅ | 0.4 s | chrome133a won |
| 3.J | Stealth visualisation | Library | ✅ | manual | screenshot attached |
| 3.K | Error taxonomy | Library | ✅ | 4 s | |
| 4.2 | Docker LLM probe | Docker | ✅ | 2 s | |
| 4.3 | All library tests in Docker | Docker | ✅ | ~5 min | |
| 5.A-F | MCP tool calls from Claude Code | MCP | ✅ | manual | |
| 5.4 | Truncation flag | MCP | ✅ | 6 s | |

### Anomalies

(list anything unexpected — wrong shape, timeout you didn't expect, …)
```

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| **`BrowserType.launch: spawn UNKNOWN`** when running Pattern E (E1 or E2) on a **Windows host** | Playwright 1.58 + Windows + `--remote-debugging-pipe` is broken on both Python 3.13 and 3.14. Scrapling pins `playwright==1.58.0` exactly so we can't bump. | Run Pattern E **inside the Docker image** (Linux runtime) instead. Patterns A / B / C / `scrapper-tool canary` all work fine on Windows host — only the browser-launching path is affected. |
| Docker container can't reach `host.docker.internal:1234` | Linux without Docker Desktop | The compose file declares `extra_hosts: ["host.docker.internal:host-gateway"]` — confirm with `docker compose run --rm scrapper-tool getent hosts host.docker.internal`. If empty, your Docker version is too old; update or use the host's actual LAN IP. |
| LM Studio probe fails with HTTP 404 | LM Studio's local server isn't enabled or is on a different port | Open LM Studio → Developer / Local Server → confirm "Server is running on http://localhost:**1234**" |
| Pattern E1 returns `"error": "schema-validation-failed"` consistently | Local LLM is producing free-form prose instead of pure JSON | Try a stricter `instruction` (e.g. "Return ONLY a JSON object, no markdown fence, no prose"), or try a larger model (`qwen3-vl-30b`) |
| Pattern E2 burns the step budget without getting anywhere | Local LLM (especially smaller variants) struggles with multi-step planning | Use a more directive instruction with explicit step-by-step language; consider a vision-capable model and confirm `use_vision` is on (auto-detected for `*vl*` model names) |
| Camoufox session crashes on first run | Patched Firefox not downloaded | Run `camoufox fetch` in your venv; for Docker, rebuild with `--build-arg INSTALL_CAMOUFOX=1` |
| Patchright fails to launch in Docker with "Host system is missing dependencies" | One of the X / fonts libs missing | Re-run `patchright install chromium --with-deps` inside the container; the default Dockerfile already does this at build time |
| MCP tool returns `"error": "scrapper-tool[llm-agent] extra not installed"` | Your local install is missing `[llm-agent]` | `uv pip install scrapper-tool[full,agent]` and restart the MCP client |
| `AgentResult.tokens_used == 0` | LM Studio doesn't return usage counts in the OpenAI-compat shape | Cosmetic only. Doesn't affect functionality |

---

## 11. After the run

- Stash your `.env` (it has the LLM URL/model and possibly a CapSolver key — don't commit it).
- Stop LM Studio's local server (LM Studio → Developer → Stop Server) to free VRAM.
- `docker compose down` (no-op since we never `up`'d, but won't hurt).
- File a GitHub issue if you hit unexpected failures, attaching the
  reporting template above. Use the `e2e-report` label.
